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


# --- the 72-hour rule ---------------------------------------------------------


@pytest.mark.parametrize("delta,expected", [
    (timedelta(hours=72), True),                       # exactly 72h: eligible
    (timedelta(hours=72, seconds=1), True),
    (timedelta(hours=71, minutes=59), False),          # just short: not yet
    (timedelta(hours=1), False),
    (timedelta(days=10), True),
])
def test_older_than_72_hours(delta, expected):
    assert policy.older_than(NOW - delta, 72, NOW) is expected


def test_older_than_excludes_unparseable_creation_time():
    assert policy.older_than(None, 72, NOW) is False


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
def test_poll_action_covers_every_documented_state(state, expected):
    assert policy.poll_action(state) == expected


def test_poll_action_handles_all_eight_states():
    """If Graph ever adds a ninth state we want the default (write nothing), not
    a KeyError -- but the eight documented ones must all be accounted for."""
    assert len(policy.ALL_JOB_STATES) == 8
    for state in policy.ALL_JOB_STATES:
        assert policy.poll_action(state) in (
            policy.POLL_COMPLETE, policy.POLL_FAIL, policy.POLL_NONE)


@pytest.mark.parametrize("state,expected", [
    ("completed", policy.RESUBMIT_MARK_COMPLETED),
    ("pending", policy.RESUBMIT_SKIP),
    ("processing", policy.RESUBMIT_SKIP),
    ("canceled", policy.RESUBMIT_RESUBMIT),
    ("aborted", policy.RESUBMIT_RESUBMIT),
    ("paused", policy.RESUBMIT_CANCEL_THEN_RESUBMIT),
    ("stopped", policy.RESUBMIT_CANCEL_THEN_RESUBMIT),
    ("unknown", policy.RESUBMIT_CANCEL_THEN_RESUBMIT),
])
def test_resubmit_action_covers_every_documented_state(state, expected):
    assert policy.resubmit_action(state) == expected


def test_a_stopped_job_must_be_cancelled_before_reprinting():
    """THE double-print guard.

    Graph defines `stopped` as "an issue with the printer needs to be addressed
    BEFORE THE JOB CAN CONTINUE" -- the job is alive. Reprinting without
    cancelling means the original and the replacement both come out once someone
    clears the jam. This assertion is the reason cancel_job exists.
    """
    assert policy.resubmit_action("stopped") == policy.RESUBMIT_CANCEL_THEN_RESUBMIT


def test_an_unrecognised_state_is_treated_conservatively():
    """Anything the service invents later gets cancel-then-resubmit: safe for
    correctness (no duplicate) at the cost of one wasted cancel call."""
    assert policy.resubmit_action("teleporting") == policy.RESUBMIT_CANCEL_THEN_RESUBMIT
    assert policy.poll_action("teleporting") == policy.POLL_NONE
    assert policy.poll_action(None) == policy.POLL_NONE


@pytest.mark.parametrize("state", ["COMPLETED", " Completed ", "cOmPlEtEd"])
def test_state_matching_is_case_and_space_insensitive(state):
    assert policy.poll_action(state) == policy.POLL_COMPLETE


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


def test_window_and_age_defaults_match_the_requirement():
    assert policy.resolve_window_days() == 20
    assert policy.resolve_min_age_hours() == 72


@pytest.mark.parametrize("resolver,bad", [
    (policy.resolve_window_days, 0),
    (policy.resolve_window_days, 400),
    (policy.resolve_min_age_hours, 0),
    (policy.resolve_min_age_hours, 100000),
])
def test_window_and_age_range_validation(resolver, bad):
    with pytest.raises(ValueError):
        resolver(bad)


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


def test_resubmit_scope_excludes_ready_and_completed():
    """The requirement stated the scope two ways; this pins the one in force.
    Including PRINT_READY would duplicate Submit's job (docs/design.md R17)."""
    assert policy.RESUBMIT_STATUSES == (policy.PENDING, policy.FAILED)
    assert policy.READY not in policy.RESUBMIT_STATUSES
    assert policy.COMPLETED not in policy.RESUBMIT_STATUSES
