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
           ack_age_minutes=None, spent_at_minutes=None, stall_minutes=5,
           give_up_days=10):
    """poll_decision in the units the schedule is actually specified in.

    `spent_at_minutes` is the file's age when the current attempt began, which is
    what encodes how many retries are already spent. It defaults to 0 -- a first
    attempt, claimed the moment the file appeared, with nothing spent yet.

    `ack_age_minutes` is printJob.acknowledgedDateTime -- when the PRINTER took
    the job, which is what the stall threshold is measured from when the job
    carries one. Absent by default, which is the shape every test written before
    2026-09-07 assumed and which must keep behaving exactly as it did.
    """
    file_created = NOW - timedelta(minutes=file_age_minutes)
    job_created = (None if job_age_minutes is None
                   else NOW - timedelta(minutes=job_age_minutes))
    job_acknowledged = (None if ack_age_minutes is None
                        else NOW - timedelta(minutes=ack_age_minutes))
    started_at = 0 if spent_at_minutes is None else spent_at_minutes
    return policy.poll_decision(
        state,
        file_created=file_created,
        has_job=job_age_minutes is not None or state not in ("", None),
        job_created=job_created,
        job_acknowledged=job_acknowledged,
        attempt_started=NOW - timedelta(minutes=file_age_minutes - started_at),
        now=NOW,
        stall_minutes=stall_minutes, give_up_days=give_up_days,
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


@pytest.mark.parametrize("state", ["paused", "stopped", "unknown", "processing"])
def test_every_non_terminal_state_can_stall(state):
    """THE double-print guard, at the rules layer.

    None of these is documented as dead, so any of them may still print. A
    requeue therefore has to cancel first -- which is exactly what POLL_REQUEUE
    tells the caller to do. Reprinting without the cancel means the original and
    the replacement both come out once someone clears the jam.

    `pending` USED TO BE IN THIS LIST and was removed on 2026-09-07 (R24). It is
    the one state that says the printer has NOT taken the job, so there is nothing
    stuck to cancel -- see the tests below.
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


# --- `pending` is not a stall (R24) -------------------------------------------
# Graph documents `pending` as "The print job is pending processing by the
# printer" -- the device has NOT taken it. There is nothing stuck to cancel, and
# cancelling would kill a job that is merely waiting its turn, which is how a
# healthy queue turned into a cancel-and-reprint loop before 2026-09-07.


def test_a_pending_job_is_never_stalled_however_long_it_waits():
    assert policy.job_is_stalled("pending", True, 10_000, 5) is False


def test_a_row_with_no_job_is_stalled_even_if_a_state_says_pending():
    """THE ORDER OF THE TWO CHECKS, pinned. `has_job` is tested BEFORE the state,
    so a row Poll cannot find a job for is requeued whatever a stale state string
    claims -- that is rule 1's recovery, and losing it strands the document.

    Today the route cannot produce this input: it only reads a state when it has a
    job, so `has_job=False` always arrives with an empty state, and reversing the
    two checks changes nothing observable. That is exactly why this test exists --
    it pins the ordering against a future where a 404 keeps the last known state.
    """
    assert policy.job_is_stalled("pending", False, None, 5) is True
    assert policy.job_is_stalled("", False, None, 5) is True


def test_a_pending_job_past_the_threshold_is_left_alone():
    action, _ = decide("pending", file_age_minutes=60.0, job_age_minutes=60.0)
    assert action == policy.POLL_NONE


def test_a_pending_job_past_the_give_up_deadline_is_still_failed():
    """The exemption cannot strand a row for ever: the give-up test runs BEFORE
    the stall test, so `giveUpDays` still bounds a job the printer never took --
    and the caller still cancels it first."""
    action, _ = decide("pending", file_age_minutes=11 * 24 * 60,
                       job_age_minutes=60.0)
    assert action == policy.POLL_GIVE_UP


# --- the stall clock: the printer's own acknowledgement, when there is one -----


def test_the_stall_clock_prefers_the_acknowledgement():
    """A job created three hours ago but acknowledged a minute ago has been with
    the PRINTER for one minute. Measuring from creation would find it stalled on
    the very first poll after it started printing."""
    action, _ = decide("processing", file_age_minutes=180.0,
                       job_age_minutes=180.0, ack_age_minutes=1.0)
    assert action == policy.POLL_NONE


def test_an_acknowledged_job_still_stalls_once_the_printer_has_held_it_too_long():
    action, _ = decide("processing", file_age_minutes=180.0,
                       job_age_minutes=180.0, ack_age_minutes=10.0)
    assert action == policy.POLL_REQUEUE


def test_without_an_acknowledgement_the_clock_falls_back_to_creation():
    """Every test written before the acknowledgement existed asserts this shape,
    so the fallback is not a courtesy -- it is the majority path."""
    action, _ = decide("stopped", file_age_minutes=60.0, job_age_minutes=60.0)
    assert action == policy.POLL_REQUEUE


def test_an_acknowledgement_older_than_the_job_is_ignored():
    """Incoherent -- a printer cannot acknowledge a job before it exists -- and
    taking it at face value would cancel a ONE-MINUTE-OLD job, which is the
    duplicate this whole change exists to avoid. This is one of the cells the
    naive `acknowledged or created` rule moved from "leave alone" to "cancel";
    the sweep that counted them is in docs/ai/troubleshooting.md."""
    action, _ = decide("processing", file_age_minutes=60.0, job_age_minutes=1.0,
                       ack_age_minutes=60.0)
    assert action == policy.POLL_NONE


def test_an_acknowledgement_alone_does_not_create_an_age():
    """No `createdDateTime` means the age cannot be established, and the
    abstention that guards against cancelling a job which might be printing right
    now stands. An acknowledgement must not quietly supply the age it refused."""
    assert policy.stall_clock_start(NOW - timedelta(minutes=60), None) is None
    action, _ = decide("processing", file_age_minutes=60.0,
                       job_age_minutes=None, ack_age_minutes=60.0)
    assert action == policy.POLL_NONE


def test_the_retry_number_is_still_read_off_the_creation_stamp():
    """R16 survives the clock change. The acknowledgement moves the STALL clock
    only; how many retries are already spent is still the file's age when the JOB
    was created, because that is what a requeue resets."""
    action, attempt = decide("processing", file_age_minutes=100.0,
                             job_age_minutes=20.0, ack_age_minutes=6.0,
                             spent_at_minutes=80.0)
    assert action == policy.POLL_REQUEUE
    # 80 minutes of file age when this attempt began: retries 1-4 had fallen due
    # (5, 15, 35, 75 min), so the fifth is the one being scheduled.
    assert attempt == policy.retries_due(80.0, 5) + 1 == 5


@pytest.mark.parametrize("state", ["COMPLETED", " Completed ", "cOmPlEtEd"])
def test_state_matching_is_case_and_space_insensitive(state):
    action, _ = decide(state, file_age_minutes=1.0, job_age_minutes=1.0)
    assert action == policy.POLL_COMPLETE


@pytest.mark.parametrize("state", ["PENDING", " Pending ", "pEnDiNg"])
def test_the_exemption_matches_the_state_the_same_way_everything_else_does(state):
    """Comparing the RAW string would silently re-enable the cancel the moment the
    service returned a capital P. Every other state test in this file normalises;
    the exemption has to match, or the guard is one casing change from gone."""
    action, _ = decide(state, file_age_minutes=60.0, job_age_minutes=60.0)
    assert action == policy.POLL_NONE


# --- the exponential retry schedule -------------------------------------------

# base * (2**n - 1) for base = 5. These are the numbers the runbook quotes and
# the live stall test is timed against, so they are pinned literally rather than
# recomputed from the formula -- a test that repeats the implementation's
# arithmetic cannot catch the arithmetic being wrong.
BOUNDARIES = [5, 15, 35, 75, 155, 315, 635, 1275, 2555, 5115]


@pytest.mark.parametrize("n,minutes", list(enumerate(BOUNDARIES, start=1)))
def test_each_retry_falls_due_at_its_boundary(n, minutes):
    assert policy.retries_due(minutes, 5) == n
    assert policy.retries_due(minutes - 0.001, 5) == n - 1


def test_the_ladder_continues_past_the_old_maximum():
    """`maxRetries` used to stop the count at 10, about 3d 13h. It is gone --
    giveUpDays is the only bound now -- so the eleventh retry is reachable, and
    inside a ten-day window it is the last one that is."""
    assert policy.retries_due(10 * 24 * 60, 5) == 11
    assert 5 * (2 ** 11 - 1) < 10 * 24 * 60 < 5 * (2 ** 12 - 1)


def test_retries_due_terminates_on_an_absurd_age():
    """The cap was added to stop 'the arithmetic running away on an ancient row'.
    It cannot: the ladder doubles, so the loop is logarithmic in the age. This is
    the assertion that lets the cap stay deleted."""
    assert policy.retries_due(10_000_000, 5) == 20
    # A year-9999 timestamp at the smallest legal base -- the worst case that can
    # physically be constructed, and still trivial.
    assert policy.retries_due(4_200_000_000, 1) == 31


def test_retries_due_is_monotonic():
    previous = 0
    for minutes in range(0, 6000, 7):
        current = policy.retries_due(minutes, 5)
        assert current >= previous, "the count may never go backwards"
        previous = current


def test_nothing_is_due_before_the_first_boundary():
    assert policy.retries_due(0, 5) == 0
    assert policy.retries_due(4.9, 5) == 0
    assert policy.retries_due(None, 5) == 0


def test_the_schedule_rescales_with_the_stall_threshold():
    """One knob. A one-minute threshold gives 1, 3, 7, 15 ... minutes."""
    assert [policy.retries_due(m, 1) for m in (1, 3, 7, 15)] == [1, 2, 3, 4]


# --- the ladder as a TIME: next_retry_time ------------------------------------


@pytest.mark.parametrize("n,minutes", list(enumerate(BOUNDARIES, start=1)))
def test_next_retry_time_lands_on_the_boundary(n, minutes):
    """The same ladder `retries_due` counts, expressed as an instant."""
    created = NOW - timedelta(days=1)
    assert policy.next_retry_time(created, 5, n, 10) == created + timedelta(
        minutes=minutes)


@pytest.mark.parametrize("n,minutes", list(enumerate(BOUNDARIES, start=1)))
def test_the_count_and_the_time_agree(n, minutes):
    """The two definitions of the schedule must not drift apart: the instant
    next_retry_time returns for retry n is exactly the age at which retries_due
    starts answering n."""
    created = NOW - timedelta(days=30)
    due = policy.next_retry_time(created, 5, n, 365)
    assert policy.retries_due((due - created).total_seconds() / 60.0, 5) == n


def test_next_retry_time_is_clamped_to_the_give_up_deadline():
    """THE ONLY UPPER BOUND. Retry 12 would fall at 20475 minutes, four days past
    a ten-day deadline -- and a row waiting that long sits at PRINT_READY, where
    Poll cannot see it to fail it. Clamped, the last attempt lands ON the
    deadline instead."""
    created = NOW - timedelta(days=1)
    deadline = created + timedelta(days=10)
    assert 5 * (2 ** 12 - 1) > 10 * 24 * 60, "retry 12 is outside the window"
    assert policy.next_retry_time(created, 5, 12, 10) == deadline
    assert policy.next_retry_time(created, 5, 40, 10) == deadline


def test_next_retry_time_survives_a_created_date_near_the_end_of_time():
    """2**retry_number can overflow datetime for a creation date near year 9999.
    The clamp is the answer anyway, so it is returned rather than raised."""
    created = datetime(9999, 1, 1, tzinfo=timezone.utc)
    assert policy.next_retry_time(created, 1440, 60, 1) == created + timedelta(days=1)


def test_next_retry_time_without_a_creation_time_is_unknowable():
    assert policy.next_retry_time(None, 5, 3, 10) is None


# --- is_due -------------------------------------------------------------------


def test_a_blank_schedule_means_due_now():
    """Files arrive from upstream with no Print_Time, and a human resetting a
    terminal row leaves none either. Fail-closed would strand both."""
    assert policy.is_due(None, NOW) is True


def test_a_past_schedule_is_due_and_a_future_one_is_not():
    assert policy.is_due(NOW - timedelta(seconds=1), NOW) is True
    assert policy.is_due(NOW, NOW) is True, "the boundary is inclusive"
    assert policy.is_due(NOW + timedelta(seconds=1), NOW) is False


def test_a_stalled_job_is_requeued_at_once_and_the_backoff_becomes_a_due_time():
    """THE BACKOFF, RELOCATED. Two retries are already spent (the attempt began at
    27 minutes) and the third is not due until 35. This case used to answer
    POLL_NONE at 32 minutes: the row waited at PRINT_PENDING while Poll declined it
    on every run, and the backoff was a comparison nobody could see.

    Now the row leaves PRINT_PENDING immediately and carries the wait with it, as
    the retry-3 due time the caller writes into Print_Time. The schedule is
    unchanged -- the third attempt still happens at 35 minutes -- but it is
    written down, and Submit is what honours it.
    """
    created = NOW - timedelta(minutes=32)
    action, attempt = decide(file_age_minutes=32, job_age_minutes=5,
                             spent_at_minutes=27)
    assert action == policy.POLL_REQUEUE
    assert attempt == 3, "the retry being SCHEDULED, not the one already spent"
    assert policy.next_retry_time(created, 5, attempt, 10) == created + timedelta(
        minutes=35), "still the 35-minute boundary, three minutes from now"

    # Eight minutes later the same file is past that boundary, so the identical
    # decision now yields a due time in the past -- print immediately.
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


def test_work_continues_right_up_to_the_give_up_deadline():
    """THE GRACE PERIOD IS GONE, deliberately. `maxRetries` used to stop new jobs
    at about 3d 13h and leave the last one live until day 10. With one bound left,
    a stalled row deep in the schedule is still requeued -- and the clamp is what
    keeps that from scheduling anything past the deadline."""
    action, attempt = decide(file_age_minutes=6000, job_age_minutes=600,
                             spent_at_minutes=6000)
    assert action == policy.POLL_REQUEUE

    created = NOW - timedelta(minutes=6000)
    assert policy.next_retry_time(created, 5, attempt, 10) <= created + timedelta(
        days=10), "never scheduled past the moment the row will be failed"


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
        stall_minutes=5, give_up_days=10)
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
    assert "1825" in policy.requeue_message("1825", 3, policy.CANCEL_OK)


def test_the_give_up_message_says_how_many_attempts_and_how_long():
    """PRINT_FAILED is terminal and a human acts on it, so the last thing written
    has to be worth reading."""
    message = policy.give_up_message(9, 10, policy.CANCEL_OK)
    assert "9" in message and "10" in message


# --- what the cancel actually achieved ----------------------------------------
# These exist because the messages used to say "cancelled" unconditionally while
# the caller held -- and discarded -- the answer. Confirmed live on 2026-09-02:
# Print_Message read "Job Id 38 cancelled" while the portal read `stopped`.


def test_a_requeue_message_only_claims_a_cancel_that_happened():
    ok = policy.requeue_message("38", 11, policy.CANCEL_OK)
    failed = policy.requeue_message("38", 11, policy.CANCEL_FAILED)

    assert "cancelled" in ok
    assert "CANCEL FAILED" in failed
    assert "cancelled" not in failed, \
        "the audit trail must not claim a cancel that did not take"


def test_a_requeue_message_does_not_claim_a_cancel_when_there_was_no_job():
    """A crashed submission (rule 1) has no job. Saying "cancelled" about nothing
    is noise in the one column a human reads to work out what happened -- and so
    is naming an id that does not exist."""
    message = policy.requeue_message("", 1, policy.CANCEL_NOTHING)
    assert message == "No job to cancel. Retry job (1)"


@pytest.mark.parametrize("result", [policy.CANCEL_OK, policy.CANCEL_FAILED])
def test_the_requeue_message_keeps_its_shape_whenever_there_was_a_job(result):
    """The job id leads and the retry number trails, either side of the cancel
    clause. Both are what a reader scans for, and Tier C asserts them.

    CANCEL_NOTHING is excluded because it cannot co-occur with a job id:
    `_cancel_outstanding` returns it only when Print_JobId is empty.
    """
    message = policy.requeue_message("1825", 3, result)
    assert "1825" in message and "Retry job (3)" in message


def test_the_give_up_message_warns_when_the_job_outlived_the_row():
    """Nothing follows a give-up: PRINT_FAILED is terminal and Print_JobId is
    gone. A job left alive prints days later against a column saying it never
    did."""
    ok = policy.give_up_message(9, 10, policy.CANCEL_OK)
    failed = policy.give_up_message(9, 10, policy.CANCEL_FAILED)

    assert "outstanding job cancelled" in ok
    assert "NOT CANCELLED" in failed and "may still print" in failed


def test_a_failed_cancel_warns_about_a_duplicate():
    warning = policy.duplicate_warning("38", policy.CANCEL_FAILED)
    assert "38" in warning and "duplicate" in warning


def test_an_accepted_cancel_the_job_ignored_is_reported_as_an_observation():
    """Cancel is asynchronous, so a job still reading `stopped` may yet settle.
    The sentence has to say what was SEEN, not rule on what it means."""
    warning = policy.duplicate_warning("39", policy.CANCEL_OK, "stopped")
    assert "39" in warning and "stopped" in warning
    assert "accepted" in warning


def test_only_a_confirmed_cancel_earns_the_word_cancelled():
    """THE DIRECTION THIS WHOLE CHANGE TURNS ON. An unrecognised result -- a typo,
    a fourth constant added without updating these -- must under-claim, not
    over-claim. Saying CANCEL FAILED about a cancel that worked costs someone a
    glance at the printer; the reverse hides a duplicate, which is what happened.
    """
    assert "cancelled" in policy.requeue_message("38", 1, policy.CANCEL_OK)
    assert "CANCEL FAILED" in policy.requeue_message("38", 1, "typo")
    assert "NOT CANCELLED" in policy.give_up_message(1, 10, "typo")
    assert policy.duplicate_warning("38", "typo") != ""


def test_the_cancel_results_are_three_distinct_values():
    assert len(set(policy.ALL_CANCEL_RESULTS)) == 3


@pytest.mark.parametrize("state_after,confirmed", [
    ("canceled", True),   # the cancel landed
    ("", True),           # Graph no longer has the job -- a 404 is success here
    ("stopped", False),   # what jobs 38 and 39 did on the live run
    ("processing", False),
    ("pending", False),
    ("COMPLETED", False),  # it printed: emphatically not a confirmed cancel
])
def test_only_a_dead_job_confirms_a_cancel(state_after, confirmed):
    """The predicate the route asks after re-reading a cancelled job. It lives in
    print_policy because a job state is a rule, and function_app holds none."""
    assert policy.cancel_confirmed(state_after) is confirmed


@pytest.mark.parametrize("result", [policy.CANCEL_OK, policy.CANCEL_NOTHING])
def test_no_warning_when_no_duplicate_is_possible(result):
    """A clean cancel and an absent job are both silence. A warning that cries
    on the ordinary path is one nobody reads on the day it matters."""
    assert policy.duplicate_warning("1825", result, "") == ""


# --- why Poll left a row alone ------------------------------------------------


def test_the_still_running_message_carries_the_stall_clock():
    message = policy.still_running_message("processing", 3.2, 5)
    assert "processing" in message and "3.2" in message and "5" in message


def test_a_pending_row_is_not_described_against_a_threshold_it_cannot_reach():
    """`pending` no longer stalls, so quoting "(stall 5)" at it would describe a
    deadline that will never fire and send someone looking for a bug. Say what is
    actually true: the printer has not taken it yet."""
    message = policy.still_running_message("pending", 42.0, 5)
    assert "pending" in message and "42.0" in message
    assert "stall" not in message


def test_a_pending_row_with_no_readable_age_still_says_why_it_was_left():
    """The `pending` job whose createdDateTime is missing or unreadable. It was
    reached by an existing test but nothing checked what it SAID, so the sentence
    was free to drift into nonsense. It must not borrow the "age unknown - not
    stalled" wording either: that one explains an ABSTENTION, and this row is not
    an abstention -- it would be left alone at any age."""
    message = policy.still_running_message("pending", None, 5)
    assert "pending" in message
    assert "stall" not in message and "age unknown" not in message
    assert "printer has not taken it" in message


def test_a_job_whose_age_is_unknown_says_so_rather_than_looking_healthy():
    """THE CASE THIS MESSAGE EXISTS FOR. `job_is_stalled` abstains on an
    undeterminable age, so the row sits at PRINT_PENDING for ever. It used to
    report the same bare word as a job ten seconds old."""
    message = policy.still_running_message("processing", None, 5)
    assert "age unknown" in message and "not stalled" in message


def test_no_job_still_reads_as_no_job():
    assert policy.still_running_message("", None, 5) == "no job"


# --- Print_Time on the wire ---------------------------------------------------


def test_the_due_time_is_written_in_business_local_time():
    """The column exists to be read by a person in SharePoint, so it is rendered
    where they live. August in Vancouver is PDT, UTC-7."""
    assert policy.format_business_datetime(NOW) == "2026-08-30T14:00:00-07:00"


def test_the_due_time_always_carries_its_offset():
    """THE BUG GUARD. This value is read back and compared, unlike the printed-on
    message, and parse_graph_datetime assumes a naive string is UTC -- so dropping
    the offset would misread every due time by seven or eight hours."""
    for when in (NOW, datetime(2026, 1, 15, 20, 0, tzinfo=UTC)):
        rendered = policy.format_business_datetime(when)
        assert rendered.endswith(("-07:00", "-08:00")), rendered


@pytest.mark.parametrize("when", [
    datetime(2026, 1, 15, 20, 0, tzinfo=UTC),   # PST, UTC-8
    datetime(2026, 7, 15, 20, 0, tzinfo=UTC),   # PDT, UTC-7
])
def test_the_due_time_round_trips_to_the_same_instant(when):
    """Written locally, read back as UTC, unchanged -- on both sides of the
    daylight-saving switch, where a naive value would be wrong by an amount that
    itself changes with the season."""
    assert policy.parse_graph_datetime(
        policy.format_business_datetime(when)) == when


@pytest.mark.parametrize("tz,offset", [
    ("Asia/Kolkata", "+05:30"),
    ("Asia/Kathmandu", "+05:45"),
    ("Pacific/Chatham", "+12:45"),
])
def test_a_fractional_hour_offset_survives_the_round_trip(tz, offset):
    """Not every zone is a whole number of hours from UTC, and the obvious
    "simplification" -- strftime("%z") -- emits +0530 without the colon. This pins
    the ISO form, which is what parse_graph_datetime and Graph both expect."""
    when = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)
    rendered = policy.format_business_datetime(when, tz_name=tz)

    assert rendered.endswith(offset), rendered
    assert policy.parse_graph_datetime(rendered) == when


def test_a_due_time_is_truncated_to_whole_seconds_and_never_rounded_up():
    """SharePoint's createdDateTime carries milliseconds, so a computed due time
    does too, and the column is written to second precision. Truncating makes a
    file due a fraction of a second EARLY; rounding up would make it late, and a
    schedule that drifts later on every hop would compound."""
    created = policy.parse_graph_datetime("2026-09-02T14:23:23.456Z")
    due = policy.next_retry_time(created, 5, 3, 10)

    written_back = policy.parse_graph_datetime(
        policy.format_business_datetime(due))

    assert written_back <= due
    assert due - written_back < timedelta(seconds=1)


def test_an_unknown_due_time_is_written_as_empty():
    """Which is what clears the column, and what is_due reads as 'print now'."""
    assert policy.format_business_datetime(None) == ""


def test_the_due_time_falls_back_to_utc_without_a_time_zone_database():
    """tzdata is a hard requirement, but a reporting time zone must never be able
    to fail a print run -- and the offset survives even in the fallback."""
    rendered = policy.format_business_datetime(NOW, tz_name="Mars/Olympus_Mons")
    assert rendered == "2026-08-30T21:00:00+00:00"


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


def test_the_retry_knob_defaults():
    assert policy.resolve_give_up_days() == 10
    assert policy.resolve_stall_minutes() == 5


@pytest.mark.parametrize("resolver,bad", [
    (policy.resolve_give_up_days, 0),
    (policy.resolve_give_up_days, 400),
    (policy.resolve_stall_minutes, 0),
    (policy.resolve_stall_minutes, 2000),
])
def test_retry_knob_range_validation(resolver, bad):
    """An out-of-range REQUEST value raises, which function_app._tunable turns
    into a 400 -- the caller's error, not a server fault."""
    with pytest.raises(ValueError):
        resolver(bad)


@pytest.mark.parametrize("resolver,env,default", [
    (policy.resolve_batch_size, "PRINT_BATCH_SIZE", policy.DEFAULT_BATCH_SIZE),
    (policy.resolve_give_up_days, "PRINT_GIVE_UP_DAYS", policy.DEFAULT_GIVE_UP_DAYS),
    (policy.resolve_stall_minutes, "PRINT_STALL_MINUTES",
     policy.DEFAULT_STALL_MINUTES),
])
def test_an_app_setting_no_longer_influences_a_tunable(monkeypatch, resolver, env,
                                                       default):
    """THE FALLBACK IS GONE, AND THIS IS WHAT PROVES IT. Each of these resolved
    request > app setting > default, which split the answer to 'why is the pacing
    wrong?' across two places with the flow silently winning -- so a setting could
    be read, believed, and be doing nothing.

    A leftover value in the environment must now change nothing at all. That
    matters most during the deploy, when the settings still exist on the Function
    App and are only deleted once the flows are confirmed green."""
    monkeypatch.setenv(env, "7")
    assert resolver() == default, "the environment is not consulted"
    assert resolver(3) == 3, "the request body still decides"


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
    # The fifth: when the next attempt falls due. Every one of these names is a
    # column somebody has to create in SharePoint by hand, so the count is pinned
    # -- adding one is a deployment step, not just a code change.
    assert policy.COLUMN_PRINT_TIME == "Print_Time"
    assert len(policy.COLUMN_DISPLAY_NAMES) == 5


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
