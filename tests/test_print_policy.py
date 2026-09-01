"""
test_print_policy.py — Tier A: the rules, offline, no network, no Azure SDK.

These are the assertions that stop the pipeline from quietly doing the wrong
thing: the 72-hour boundary, the job-state mapping that decides whether a
document reprints, and the time-zone split between windowing (UTC) and display
(business local). All of it runs in well under a second.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

import print_policy as policy

UTC = timezone.utc
NOW = datetime(2026, 8, 30, 21, 0, 0, tzinfo=UTC)


# --- structural: the constraint that makes reuse possible ---------------------


def test_print_policy_imports_only_the_standard_library():
    """print_policy must never import an adapter or a cloud SDK.

    This is the single structural rule the whole design leans on: it is what
    keeps the suite offline and what lets another workflow keep the adapters and
    replace only the rules. A violation here means the layering has collapsed,
    so it is asserted rather than trusted.
    """
    source = pathlib.Path(policy.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                imported.add(node.module.split(".")[0])

    forbidden = imported - set(sys.stdlib_module_names)
    assert not forbidden, (
        "print_policy.py must import only the standard library; found: "
        + ", ".join(sorted(forbidden))
    )


# --- the 20-day window --------------------------------------------------------


@pytest.mark.parametrize("age_days,expected", [
    (0, True), (1, True), (19.9, True),
    (20.1, False), (21, False), (400, False),
])
def test_within_window(age_days, expected):
    created = NOW - timedelta(days=age_days)
    assert policy.within_window(created, 20, NOW) is expected


def test_within_window_boundary_is_inclusive():
    """Exactly 20 days old is still in scope. An exclusive boundary would drop a
    file on the precise tick it aged out, which is untestable in production and
    looks like data loss."""
    assert policy.within_window(NOW - timedelta(days=20), 20, NOW) is True


def test_within_window_excludes_unparseable_creation_time():
    """A row whose creation time cannot be read is out of scope. It cannot be
    SHOWN to be in the window, and silently including it would let an unbounded
    set through."""
    assert policy.within_window(None, 20, NOW) is False


def test_windows_are_computed_in_utc_across_a_dst_change():
    """The windows must be wall-clock-independent.

    Vancouver leaves daylight saving on 2026-11-01, so a 20-day span across that
    date is 20*24 hours in UTC but would be 20*24+1 in local time. Computing in
    UTC is what keeps the boundary stable; this asserts the arithmetic never
    touches a local calendar.
    """
    after_dst = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    exactly_20_days = after_dst - timedelta(days=20)     # 2026-10-21, before the change
    assert policy.within_window(exactly_20_days, 20, after_dst) is True
    assert policy.within_window(exactly_20_days - timedelta(seconds=1),
                                20, after_dst) is False


# --- Universal Print state mapping -------------------------------------------


def decide(state="stopped", *, file_age_minutes=1.0, job_age_minutes=None,
           spent_at_minutes=None, stall_minutes=5, give_up_days=10,
           max_retries=10):
    """poll_decision in the units the schedule is actually specified in.

    `spent_at_minutes` is the file's age when the current attempt began, which is
    what encodes how many retries are already spent. It defaults to 0 -- a first
    attempt, claimed the moment the file appeared, with nothing spent yet.
    """
    file_created = NOW - timedelta(minutes=file_age_minutes)
    job_created = (None if job_age_minutes is None
                   else NOW - timedelta(minutes=job_age_minutes))
    started_at = 0 if spent_at_minutes is None else spent_at_minutes
    return policy.poll_decision(
        state,
        file_created=file_created,
        has_job=job_age_minutes is not None or state not in ("", None),
        job_created=job_created,
        attempt_started=NOW - timedelta(minutes=file_age_minutes - started_at),
        now=NOW,
        stall_minutes=stall_minutes, give_up_days=give_up_days,
        max_retries=max_retries,
    )


@pytest.mark.parametrize("state,expected", [
    ("completed", policy.POLL_COMPLETE),
    ("canceled", policy.POLL_FAIL),
    ("aborted", policy.POLL_FAIL),
    ("pending", policy.POLL_NONE),
    ("processing", policy.POLL_NONE),
    ("paused", policy.POLL_NONE),
    ("stopped", policy.POLL_NONE),
    ("unknown", policy.POLL_NONE),
])
def test_poll_decision_covers_every_documented_state(state, expected):
    """A one-minute-old job on a one-minute-old file: nothing is stalled yet, so
    only the terminal states produce a write."""
    action, _ = decide(state, file_age_minutes=1.0, job_age_minutes=1.0)
    assert action == expected


def test_poll_decision_handles_all_eight_states():
    """If Graph ever adds a ninth state we want a defined answer, not a KeyError
    -- but the eight documented ones must all be accounted for."""
    assert len(policy.ALL_JOB_STATES) == 8
    for state in policy.ALL_JOB_STATES:
        action, _ = decide(state, file_age_minutes=1.0, job_age_minutes=1.0)
        assert action in (policy.POLL_COMPLETE, policy.POLL_FAIL, policy.POLL_NONE,
                          policy.POLL_REQUEUE, policy.POLL_GIVE_UP)


@pytest.mark.parametrize("state", ["paused", "stopped", "unknown", "pending",
                                   "processing"])
def test_every_non_terminal_state_can_stall(state):
    """THE double-print guard, at the rules layer.

    None of these is documented as dead, so any of them may still print. A
    requeue therefore has to cancel first -- which is exactly what POLL_REQUEUE
    tells the caller to do. Reprinting without the cancel means the original and
    the replacement both come out once someone clears the jam.
    """
    action, _ = decide(state, file_age_minutes=6.0, job_age_minutes=6.0)
    assert action == policy.POLL_REQUEUE


@pytest.mark.parametrize("state", ["completed", "canceled", "aborted"])
def test_a_terminal_state_is_never_stalled(state):
    """However old it is. There is nothing to cancel and nothing to wait for."""
    assert policy.job_is_stalled(state, True, 10_000, 5) is False


def test_an_unrecognised_state_is_treated_as_still_live():
    """Anything the service invents later is assumed able to print, so it is
    requeued (with a cancel) rather than failed -- safe for correctness at the
    cost of one wasted cancel call."""
    action, _ = decide("teleporting", file_age_minutes=6.0, job_age_minutes=6.0)
    assert action == policy.POLL_REQUEUE


@pytest.mark.parametrize("state", ["COMPLETED", " Completed ", "cOmPlEtEd"])
def test_state_matching_is_case_and_space_insensitive(state):
    action, _ = decide(state, file_age_minutes=1.0, job_age_minutes=1.0)
    assert action == policy.POLL_COMPLETE


# --- the exponential retry schedule -------------------------------------------

# base * (2**n - 1) for base = 5. These are the numbers the runbook quotes and
# the live stall test is timed against, so they are pinned literally rather than
# recomputed from the formula -- a test that repeats the implementation's
# arithmetic cannot catch the arithmetic being wrong.
BOUNDARIES = [5, 15, 35, 75, 155, 315, 635, 1275, 2555, 5115]


@pytest.mark.parametrize("n,minutes", list(enumerate(BOUNDARIES, start=1)))
def test_each_retry_falls_due_at_its_boundary(n, minutes):
    assert policy.retries_due(minutes, 5, 10) == n
    assert policy.retries_due(minutes - 0.001, 5, 10) == n - 1


def test_all_ten_retries_fit_inside_the_ten_day_give_up_window():
    """With the shipped defaults it is max_retries that stops the retrying, at
    about 3d 13h, not the calendar. The remaining 6.5 days are the grace period
    in which the final job may still print."""
    assert BOUNDARIES[-1] < 10 * 24 * 60
    assert policy.retries_due(10 * 24 * 60, 5, 10) == 10


def test_retries_due_is_capped_at_the_maximum():
    """However ancient the row, the arithmetic must not run away."""
    assert policy.retries_due(10_000_000, 5, 10) == 10
    assert policy.retries_due(10_000_000, 5, 3) == 3


def test_retries_due_is_monotonic():
    previous = 0
    for minutes in range(0, 6000, 7):
        current = policy.retries_due(minutes, 5, 10)
        assert current >= previous, "the count may never go backwards"
        previous = current


def test_nothing_is_due_before_the_first_boundary():
    assert policy.retries_due(0, 5, 10) == 0
    assert policy.retries_due(4.9, 5, 10) == 0
    assert policy.retries_due(None, 5, 10) == 0


def test_the_schedule_rescales_with_the_stall_threshold():
    """One knob. A one-minute threshold gives 1, 3, 7, 15 ... minutes."""
    assert [policy.retries_due(m, 1, 10) for m in (1, 3, 7, 15)] == [1, 2, 3, 4]


def test_a_requeue_needs_a_NEWLY_crossed_boundary():
    """THE BACKOFF. Two retries are already spent (the attempt began at 27
    minutes); the third is not due until 35. At 32 minutes the job is stalled and
    retries remain, and still nothing happens."""
    action, _ = decide(file_age_minutes=32, job_age_minutes=5,
                       spent_at_minutes=27)
    assert action == policy.POLL_NONE

    action, attempt = decide(file_age_minutes=40, job_age_minutes=13,
                             spent_at_minutes=27)
    assert action == policy.POLL_REQUEUE
    assert attempt == 3


def test_a_job_inside_the_stall_threshold_is_not_stalled():
    action, _ = decide(file_age_minutes=100, job_age_minutes=4)
    assert action == policy.POLL_NONE


def test_a_missing_job_is_stalled_immediately():
    """A crashed submission or a purged job: nothing is coming, so there is no
    age to wait on."""
    assert policy.job_is_stalled("", False, None, 5) is True


def test_a_job_whose_age_is_unknown_is_not_stalled():
    """Cancelling a job we cannot age could kill one that is printing right now
    and queue a second copy. Abstaining only delays a retry."""
    assert policy.job_is_stalled("stopped", True, None, 5) is False


# --- the grace period and giving up -------------------------------------------


def test_spent_retries_stop_the_requeue_without_failing_the_row():
    """THE GRACE PERIOD: no more work, but the row stays PRINT_PENDING so its
    last job can still print and be recorded."""
    action, _ = decide(file_age_minutes=6000, job_age_minutes=600,
                       spent_at_minutes=6000, max_retries=2)
    assert action == policy.POLL_NONE


def test_past_the_give_up_threshold_the_row_is_failed():
    action, _ = decide(file_age_minutes=11 * 24 * 60, job_age_minutes=600)
    assert action == policy.POLL_GIVE_UP


def test_completion_beats_the_give_up_threshold():
    """ORDER OF CHECKS. The paper came out; a row saying PRINT_FAILED about a
    document that printed is worse than a late success."""
    action, _ = decide("completed", file_age_minutes=99 * 24 * 60,
                       job_age_minutes=600)
    assert action == policy.POLL_COMPLETE


def test_a_row_with_no_creation_time_is_left_alone():
    """It cannot be aged, so neither the give-up test nor the schedule means
    anything for it. Guessing would either abandon a live document or retry one
    forever."""
    action, _ = policy.poll_decision(
        "stopped", file_created=None, has_job=True,
        job_created=NOW - timedelta(minutes=600), attempt_started=None, now=NOW,
        stall_minutes=5, give_up_days=10, max_retries=10)
    assert action == policy.POLL_NONE


# --- appending to Print_Message -----------------------------------------------


def test_append_message_keeps_the_earlier_entry():
    result = policy.append_message("convert: bad PDF", "Job Id 7 cancelled")
    assert "convert: bad PDF" in result
    assert "Job Id 7 cancelled" in result


def test_append_message_handles_an_empty_column():
    assert policy.append_message("", "first") == "first"
    assert policy.append_message(None, "first") == "first"


def test_append_message_drops_the_OLDEST_entries_when_it_overflows():
    """255 characters is the column's limit. The newest entry is what someone is
    reading the row to understand, so it is the one that must survive."""
    existing = policy.MESSAGE_SEPARATOR.join("entry number {}".format(i)
                                             for i in range(40))
    result = policy.append_message(existing, "the newest thing")

    assert len(result) <= policy.MESSAGE_MAX_CHARS
    assert result.endswith("the newest thing")
    assert "entry number 0" not in result


def test_append_message_never_leaves_half_an_entry():
    existing = policy.MESSAGE_SEPARATOR.join("x" * 60 for _ in range(10))
    result = policy.append_message(existing, "newest")

    assert len(result) <= policy.MESSAGE_MAX_CHARS
    for part in result.split(policy.MESSAGE_SEPARATOR):
        assert part in ("x" * 60, "newest"), "entries must not be sliced"


def test_the_requeue_message_names_the_job_it_cancelled():
    """Print_JobId is cleared on requeue, so this is the only surviving record of
    which job was killed."""
    assert "1825" in policy.requeue_message("1825", 3)


def test_the_give_up_message_says_how_many_attempts_and_how_long():
    """PRINT_FAILED is terminal and a human acts on it, so the last thing written
    has to be worth reading."""
    message = policy.give_up_message(9, 10)
    assert "9" in message and "10" in message


# --- the printed-on message ---------------------------------------------------


def test_printed_on_renders_in_business_local_time():
    """August in Vancouver is PDT (UTC-7), so 21:00 UTC is 14:00 local."""
    assert policy.printed_on_message(NOW) == "printed on 2026-08-30 14:00:00"


def test_printed_on_respects_standard_time_in_winter():
    """January is PST (UTC-8): the same UTC hour renders an hour earlier. If this
    ever equals the summer answer, the code has stopped converting."""
    winter = datetime(2026, 1, 15, 21, 0, 0, tzinfo=UTC)
    assert policy.printed_on_message(winter) == "printed on 2026-01-15 13:00:00"


def test_printed_on_crosses_the_local_day_boundary():
    """05:00 UTC on the 30th is 22:00 on the 29th in Vancouver. A naive
    implementation formats the UTC date and reports the wrong DAY, which is the
    kind of error nobody notices until an audit."""
    early = datetime(2026, 8, 30, 5, 0, 0, tzinfo=UTC)
    assert policy.printed_on_message(early) == "printed on 2026-08-29 22:00:00"


def test_printed_on_matches_the_requirement_format():
    """The requirement's own example: "printed on 2026-08-01 14:23:23"."""
    import re
    message = policy.printed_on_message(NOW)
    assert re.fullmatch(r"printed on \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", message)


def test_printed_on_falls_back_to_utc_for_an_unknown_zone(caplog):
    """A bad time-zone name must not fail a print run; it warns and uses UTC."""
    message = policy.printed_on_message(NOW, tz_name="Mars/Olympus_Mons")
    assert message == "printed on 2026-08-30 21:00:00"


# --- messages -----------------------------------------------------------------


def test_truncate_message_fits_the_column():
    long_error = "x" * 1000
    result = policy.truncate_message(long_error)
    assert len(result) <= policy.MESSAGE_MAX_CHARS


def test_truncate_message_flattens_whitespace():
    assert policy.truncate_message("a\n\n  b\tc") == "a b c"


def test_truncate_message_handles_none():
    assert policy.truncate_message(None) == ""


def test_failure_message_names_the_stage():
    """A failure reading the file out of SharePoint must not read like a printer
    fault, or whoever opens the column goes to the wrong system."""
    message = policy.failure_message("download", "404 itemNotFound")
    assert message.startswith("download:")
    assert "404" in message


def test_failure_message_is_never_empty():
    """An empty Print_Message on a PRINT_FAILED row is indistinguishable from a
    row nobody has touched."""
    assert policy.failure_message("upload", "") != ""
    assert policy.failure_message("upload", None) != ""


def test_failure_message_fits_the_column_even_with_a_huge_error():
    message = policy.failure_message("create_job", "y" * 5000)
    assert len(message) <= policy.MESSAGE_MAX_CHARS


# --- selection ----------------------------------------------------------------


def _row(item_id, created):
    return {"id": item_id, "created": created}


def test_select_oldest_takes_the_oldest_first():
    rows = [
        _row("c", NOW - timedelta(days=1)),
        _row("a", NOW - timedelta(days=10)),
        _row("b", NOW - timedelta(days=5)),
    ]
    assert [r["id"] for r in policy.select_oldest(rows, 2)] == ["a", "b"]


def test_select_oldest_respects_the_batch_size():
    rows = [_row(str(i), NOW - timedelta(days=i)) for i in range(1, 30)]
    assert len(policy.select_oldest(rows, 5)) == 5
    assert len(policy.select_oldest(rows, 15)) == 15


def test_select_oldest_breaks_ties_deterministically():
    """Two files created in the same second must not swap places between runs,
    or 'which five did it take' becomes a coin flip and so does the test."""
    same = NOW - timedelta(days=3)
    rows = [_row("b", same), _row("a", same), _row("c", same)]
    first = [r["id"] for r in policy.select_oldest(rows, 2)]
    second = [r["id"] for r in policy.select_oldest(list(reversed(rows)), 2)]
    assert first == second == ["a", "b"]


def test_select_oldest_puts_unparseable_dates_last():
    """A row with no creation time must not jump the queue ahead of real work."""
    rows = [_row("no-date", None), _row("old", NOW - timedelta(days=9))]
    assert [r["id"] for r in policy.select_oldest(rows, 2)] == ["old", "no-date"]


# --- tunable resolution -------------------------------------------------------


def test_batch_size_defaults_to_five():
    assert policy.resolve_batch_size() == policy.DEFAULT_BATCH_SIZE == 5


def test_batch_size_accepts_the_requirements_fifteen():
    """The requirement says "the oldest 15"; 15 is the ceiling and remains
    reachable by asking for it explicitly."""
    assert policy.resolve_batch_size(15) == 15
    assert policy.MAX_BATCH_SIZE == 15


@pytest.mark.parametrize("bad", [0, -1, 16, 999])
def test_batch_size_out_of_range_in_a_request_raises(bad):
    """A caller asking for something impossible gets a 400, not a silent clamp."""
    with pytest.raises(ValueError, match="batchSize"):
        policy.resolve_batch_size(bad)


@pytest.mark.parametrize("bad", ["abc", "5.5", [], {}])
def test_batch_size_non_integer_in_a_request_raises(bad):
    with pytest.raises(ValueError):
        policy.resolve_batch_size(bad)


def test_batch_size_env_override(monkeypatch):
    monkeypatch.setenv("PRINT_BATCH_SIZE", "9")
    assert policy.resolve_batch_size() == 9
    # An explicit request still wins over the environment.
    assert policy.resolve_batch_size(3) == 3


@pytest.mark.parametrize("bad", ["0", "99", "not-a-number"])
def test_batch_size_bad_env_falls_back_instead_of_failing(monkeypatch, bad):
    """Server misconfiguration must not take every request down with it -- the
    opposite of how a bad REQUEST value is treated."""
    monkeypatch.setenv("PRINT_BATCH_SIZE", bad)
    assert policy.resolve_batch_size() == policy.DEFAULT_BATCH_SIZE


def test_the_retry_knob_defaults():
    assert policy.resolve_give_up_days() == 10
    assert policy.resolve_stall_minutes() == 5
    assert policy.resolve_max_retries() == 10


@pytest.mark.parametrize("resolver,bad", [
    (policy.resolve_give_up_days, 0),
    (policy.resolve_give_up_days, 400),
    (policy.resolve_stall_minutes, 0),
    (policy.resolve_stall_minutes, 2000),
    (policy.resolve_max_retries, 0),
    (policy.resolve_max_retries, 99),
])
def test_retry_knob_range_validation(resolver, bad):
    """An out-of-range REQUEST value raises, which function_app._tunable turns
    into a 400 -- the caller's error, not a server fault."""
    with pytest.raises(ValueError):
        resolver(bad)


@pytest.mark.parametrize("resolver,env", [
    (policy.resolve_give_up_days, "PRINT_GIVE_UP_DAYS"),
    (policy.resolve_stall_minutes, "PRINT_STALL_MINUTES"),
    (policy.resolve_max_retries, "PRINT_MAX_RETRIES"),
])
def test_a_request_value_beats_the_app_setting(monkeypatch, resolver, env):
    """The ordering that lets Power Automate retune the schedule with no deploy:
    request first, then the app setting, then the built-in default."""
    monkeypatch.setenv(env, "7")
    assert resolver() == 7, "the app setting is the fallback"
    assert resolver(3) == 3, "the request body wins"


@pytest.mark.parametrize("resolver,env", [
    (policy.resolve_give_up_days, "PRINT_GIVE_UP_DAYS"),
    (policy.resolve_stall_minutes, "PRINT_STALL_MINUTES"),
    (policy.resolve_max_retries, "PRINT_MAX_RETRIES"),
])
def test_a_bad_app_setting_falls_back_instead_of_failing(monkeypatch, resolver, env):
    """Server misconfiguration must not take every request down with it -- the
    opposite of how a bad REQUEST value is treated."""
    monkeypatch.setenv(env, "not-a-number")
    assert resolver() > 0


def test_budget_default_undercuts_the_connector_timeout():
    """Power Automate's HTTP action gives up around 120 s. The budget must leave
    room for the response to get back before that."""
    assert policy.resolve_budget_seconds() == 90.0
    assert policy.DEFAULT_BUDGET_SECONDS < 120


# --- folder scoping -----------------------------------------------------------


FILE_DIR = "/sites/Ops/Shared Documents/Invoices/ToPrint"


@pytest.mark.parametrize("target,expected", [
    ("/Invoices/ToPrint", True),
    ("Invoices/ToPrint", True),
    ("Invoices", True),               # a parent folder still matches
    ("ToPrint", True),
    ("/invoices/toprint", True),      # SharePoint paths are case-insensitive
    ("", True),                       # no folder means the whole library
    ("/", True),
    ("/Invoices/Archive", False),
    ("Payroll", False),
    ("ToPrint/Invoices", False),      # order matters
])
def test_folder_matching(target, expected):
    assert policy.folder_matches(FILE_DIR, target) is expected


def test_folder_matching_survives_a_renamed_library():
    """The caller supplies a short path; SharePoint returns a server-relative one
    whose prefix depends on the site and library names. Matching on a contiguous
    run of segments means renaming either one does not break the flow."""
    assert policy.folder_matches(
        "/sites/Operations/Finance Docs/Invoices/ToPrint", "/Invoices/ToPrint") is True


@pytest.mark.parametrize("value,expected", [
    ("/a/b/", "a/b"), ("a/b", "a/b"), ("", ""), (None, ""),
    ("/", ""), ("\\a\\b", "a/b"),
])
def test_normalize_folder(value, expected):
    assert policy.normalize_folder(value) == expected


# --- graph timestamp parsing --------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("2026-08-30T21:00:00Z", datetime(2026, 8, 30, 21, 0, tzinfo=UTC)),
    ("2026-08-30T21:00:00+00:00", datetime(2026, 8, 30, 21, 0, tzinfo=UTC)),
    ("2026-08-30T14:00:00-07:00", datetime(2026, 8, 30, 21, 0, tzinfo=UTC)),
    ("2026-08-30T21:00:00.1234567Z", datetime(2026, 8, 30, 21, 0, 0, 123456, tzinfo=UTC)),
])
def test_parse_graph_datetime(raw, expected):
    assert policy.parse_graph_datetime(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not a date", 42, []])
def test_parse_graph_datetime_returns_none_on_junk(raw):
    """One malformed row must not fail a whole batch."""
    assert policy.parse_graph_datetime(raw) is None


def test_parse_graph_datetime_assumes_utc_when_naive():
    parsed = policy.parse_graph_datetime("2026-08-30T21:00:00")
    assert parsed == datetime(2026, 8, 30, 21, 0, tzinfo=UTC)


# --- the vocabulary itself ----------------------------------------------------


def test_status_values_match_the_requirement():
    assert policy.READY == "PRINT_READY"
    assert policy.PENDING == "PRINT_PENDING"
    assert policy.FAILED == "PRINT_FAILED"
    assert policy.COMPLETED == "PRINT_COMPLETED"


def test_column_names_match_the_requirement():
    assert policy.COLUMN_STATUS == "Print_Status"
    assert policy.COLUMN_JOB_ID == "Print_JobId"
    assert policy.COLUMN_MESSAGE == "Print_Message"
    assert policy.COLUMN_PRINTER == "Printer_Name"
    assert len(policy.COLUMN_DISPLAY_NAMES) == 4


def test_print_failed_is_terminal():
    """Nothing in the app retries PRINT_FAILED -- a human resets the row. Poll
    only ever queries PRINT_PENDING, and only ever writes PRINT_FAILED as a final
    answer, so there is no path that picks a failed row back up.

    This replaces the old RESUBMIT_STATUSES guard. Resubmit did retry
    PRINT_FAILED; nothing does now, which is why the give-up message has to carry
    enough for someone to act on.
    """
    assert not hasattr(policy, "RESUBMIT_STATUSES")
    assert policy.FAILED not in (policy.READY, policy.PENDING)


def test_the_default_job_configuration_carries_only_copies():
    """THE guard on JOB_CONFIGURATION, relocated here when the retired
    printer's fixture was deleted.

    This constant is the DEFAULT configuration -- the one used when a printer
    accepts the document as-is and no profile overrides it. It must stay
    minimal: every extra key is a setting the device might not support, and
    letting the printer's own defaults decide colour, duplex and quality is
    what makes those printer settings rather than a release.

    A printer that needs more (the PWG-raster path sends a dozen settings)
    carries its own configuration on its profile. It does not widen this one.
    """
    assert set(policy.JOB_CONFIGURATION) == {"copies"}, (
        "JOB_CONFIGURATION grew beyond `copies`: {}. Per-printer settings "
        "belong on a printer profile, not on the shared default.".format(
            sorted(policy.JOB_CONFIGURATION)))
    assert policy.JOB_CONFIGURATION["copies"] == 1
