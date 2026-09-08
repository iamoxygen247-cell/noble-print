"""
test_poll.py — Tier C: the Poll endpoint.

Poll owns the whole lifecycle of a PRINT_PENDING row: mark the finished, requeue
the stalled, fail the hopeless. It absorbed all of that from the retired Resubmit
endpoint, so the two heaviest assertions in the suite now live here:

* test_a_stalled_job_is_cancelled_before_the_requeue -- ported from
  test_resubmit.py with the endpoint. Without the cancel, the original and the
  replacement BOTH print once the printer is fixed (defect D1, CLAUDE.md rule 2).
* test_the_cancel_uses_the_printer_id_not_the_share_id -- cancel is documented
  only on the printer route.

The restraint still matters just as much. Most of this file asserts what Poll
does NOT do: a premature completion marks a document printed that never printed,
and a premature requeue puts a second sheet in the tray.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

import pytest

import print_policy
from helpers import (NOW, POLL, SITE, as_json, iso, post, print_events, result_for,
                     run_summaries)

BODY = {**SITE, "library": "Documents", "folder": "/Invoices/ToPrint"}

PRINTED_ON = re.compile(r"^printed on \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def body(**overrides):
    merged = dict(BODY)
    merged.update(overrides)
    return merged


def pending_item(graph, item_id="1", *, job_id="1825", days_old=1, **kwargs):
    graph.add_item(item_id, status=print_policy.PENDING, job_id=job_id,
                   printer=graph.SHARE_ID, created=iso(days=days_old), **kwargs)


DAY = 24 * 60


def stalled(graph, item_id="1", *, job_id="1825", state="stopped",
            file_minutes_old=DAY, attempt_at_minutes=0, job_minutes_old=None,
            ack_minutes_old=None, **kwargs):
    """A file whose current print job has sat too long to be making progress.

    `attempt_at_minutes` is THE FILE'S AGE WHEN THE CURRENT ATTEMPT BEGAN, which
    is what encodes how many retries are already spent. 0 -- the default -- means
    a first attempt, claimed as soon as the file appeared, nothing spent. Raise
    it to simulate a file that has already been requeued.

    The job is created at that same moment and has been sitting there since,
    which is what makes it stalled. `job_minutes_old` overrides just the job's
    age, for the case where the attempt is old but its job is fresh.

    Keeping these consistent still matters, but for a different reason than it used
    to. A stalled row is now requeued unconditionally, so `attempt_at_minutes` no
    longer decides WHETHER anything happens -- it decides WHICH retry is scheduled,
    and therefore what `Print_Time` a test should expect. A one-day-old file whose
    job was created an hour ago implies eight retries already spent, so the one
    scheduled is the ninth, not the first.

    `ack_minutes_old` is printJob.acknowledgedDateTime -- when the PRINTER took the
    job, and the stall clock when the job carries one. Absent by default, which is
    the shape every caller written before 2026-09-07 assumes: the clock then falls
    back to the job's own createdDateTime and nothing about them changes.
    """
    attempt_ago = file_minutes_old - attempt_at_minutes
    job_ago = attempt_ago if job_minutes_old is None else job_minutes_old
    kwargs.setdefault("printer", graph.SHARE_ID)
    graph.add_item(item_id, status=print_policy.PENDING, job_id=job_id,
                   created=iso(minutes=file_minutes_old),
                   modified=iso(minutes=attempt_ago), **kwargs)
    graph.add_job(job_id, state=state, description="Out of paper",
                  created=iso(minutes=job_ago),
                  acknowledged=(None if ack_minutes_old is None
                                else iso(minutes=ack_minutes_old)))


# --- completion ---------------------------------------------------------------


def test_a_completed_job_marks_the_file_completed(graph, frozen_now):
    pending_item(graph)
    graph.add_job("1825", state="completed", description="Printed successfully.")

    payload = as_json(post(POLL, body()))

    assert payload["completed"] == 1
    assert graph.field("1", "Print_Status") == print_policy.COMPLETED
    assert PRINTED_ON.match(graph.field("1", "Print_Message"))


def test_the_completion_message_matches_the_requirements_example(graph, frozen_now):
    """The requirement's own example: "printed on 2026-08-01 14:23:23".
    NOW is 21:00 UTC, which is 14:00 in Vancouver."""
    pending_item(graph)
    graph.add_job("1825", state="completed")

    post(POLL, body())

    assert graph.field("1", "Print_Message") == "printed on 2026-08-30 14:00:00"


def test_completion_does_not_disturb_the_other_columns(graph, frozen_now):
    """The write matrix: Poll touches only status and message. Clearing the job
    id or the printer would destroy the audit trail."""
    pending_item(graph)
    graph.add_job("1825", state="completed")

    post(POLL, body())

    assert graph.field("1", "Print_JobId") == "1825"
    assert graph.field("1", "Printer_Name") == graph.SHARE_ID


# --- terminal failures --------------------------------------------------------


@pytest.mark.parametrize("state", ["canceled", "aborted"])
def test_a_terminal_failure_marks_the_file_failed(graph, frozen_now, state):
    """Beyond the literal requirement, and deliberately so: a canceled job will
    never print, and leaving it PRINT_PENDING makes it indistinguishable from one
    still in the queue for up to three days."""
    pending_item(graph)
    graph.add_job("1825", state=state, description="Cancelled at the device",
                  details=["userCancelled"])

    payload = as_json(post(POLL, body()))

    assert payload["failed"] == 1
    assert graph.field("1", "Print_Status") == print_policy.FAILED
    message = graph.field("1", "Print_Message")
    assert "Cancelled at the device" in message
    assert "userCancelled" in message


# --- restraint: the states Poll must leave alone ------------------------------


@pytest.mark.parametrize("state", ["pending", "processing", "paused", "unknown"])
def test_an_in_flight_job_is_not_touched(graph, frozen_now, state):
    pending_item(graph)
    graph.add_job("1825", state=state)

    payload = as_json(post(POLL, body()))

    assert payload["stillRunning"] == 1
    assert graph.field("1", "Print_Status") == print_policy.PENDING
    assert not graph.calls_to("/items/1/fields", method="PATCH")


def test_a_row_left_alone_reports_the_clock_it_was_judged_against(graph,
                                                                  frozen_now):
    """`processing` alone did not say WHY Poll declined -- a healthy young job and
    one whose age cannot be read looked identical in the response."""
    pending_item(graph)
    graph.add_job("1825", state="processing", created=iso(minutes=2))

    payload = as_json(post(POLL, body()))

    message = result_for(payload, "1")["message"]
    assert "processing" in message and "2.0 min old" in message
    assert "stall 5" in message


def test_a_row_whose_job_age_is_unknown_says_so_in_the_response(graph, frozen_now):
    """THE CASE WORTH SEEING. This row is not stalled and never will be -- it sits
    at PRINT_PENDING for ever -- and it used to report the bare word `stopped`,
    indistinguishable from a job ten seconds into its life."""
    pending_item(graph)
    graph.add_job("1825", state="stopped")  # note: no `created`

    payload = as_json(post(POLL, body()))

    assert payload["stillRunning"] == 1
    assert "age unknown" in result_for(payload, "1")["message"]


def test_a_stopped_job_is_never_marked_failed(graph, frozen_now):
    """`stopped` means the printer needs attention but the job can still
    continue, so it is not a failure. Poll requeues it rather than failing it --
    the document is recoverable and PRINT_FAILED is terminal."""
    stalled(graph, state="stopped")

    payload = as_json(post(POLL, body()))

    assert payload["failed"] == 0
    assert graph.field("1", "Print_Status") != print_policy.FAILED


def test_a_job_whose_age_cannot_be_determined_is_left_alone(graph, frozen_now):
    """A job with no createdDateTime cannot be aged, and a job that cannot be
    aged must not be cancelled. Guessing "stalled" would kill a job that may be
    printing right now and queue a second copy; abstaining only delays a retry."""
    pending_item(graph)
    graph.add_job("1825", state="stopped")  # note: no `created`

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 0
    assert graph.cancelled == []
    assert graph.field("1", "Print_Status") == print_policy.PENDING


def test_a_row_with_a_job_but_no_printer_is_reported_as_malformed(graph, frozen_now):
    """A job id is only meaningful together with its printer -- Universal Print
    job ids are per-printer, not globally unique -- so this row cannot be
    resolved and is surfaced rather than silently skipped."""
    graph.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer="", created=iso(days=1))

    payload = as_json(post(POLL, body()))

    assert payload["malformed"] == 1
    assert result_for(payload, "1")["result"] == "malformed"


# --- scoping ------------------------------------------------------------------


def test_only_pending_files_are_polled(graph, frozen_now):
    for status in (print_policy.READY, print_policy.FAILED, print_policy.COMPLETED):
        graph.add_item(status, status=status, job_id="1825",
                       printer=graph.SHARE_ID, created=iso(days=1))
    pending_item(graph, "pending")
    graph.add_job("1825", state="completed")

    payload = as_json(post(POLL, body()))

    assert [i["itemId"] for i in payload["items"]] == ["pending"]
    assert graph.field(print_policy.READY, "Print_Status") == print_policy.READY


def test_no_pending_row_is_excluded_by_age(graph, frozen_now):
    """The 20-day window used to filter rows OUT of Poll's scope, which is how a
    file could end up touched by nothing ever again (design.md G1). Every pending
    row is now considered; age decides the outcome, not whether it is looked at.

    Both of these completed. Both must be recorded, however old.
    """
    pending_item(graph, "recent", job_id="1825", days_old=19)
    pending_item(graph, "ancient", job_id="1826", days_old=25)
    graph.add_job("1825", state="completed", created=iso(days=19))
    graph.add_job("1826", state="completed", created=iso(days=25))

    payload = as_json(post(POLL, body()))

    assert payload["completed"] == 2
    assert graph.field("ancient", "Print_Status") == print_policy.COMPLETED


def test_only_files_in_the_requested_folder(graph, frozen_now):
    pending_item(graph, "in", job_id="1825",
                 folder="/sites/Ops/Shared Documents/Invoices/ToPrint")
    pending_item(graph, "out", job_id="1826",
                 folder="/sites/Ops/Shared Documents/Payroll")
    graph.add_job("1825", state="completed")
    graph.add_job("1826", state="completed")

    payload = as_json(post(POLL, body()))

    assert [i["itemId"] for i in payload["items"]] == ["in"]


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize("payload,fragment", [
    ({}, "library"),
    ({"library": "Documents"}, "folder"),
    ({"library": "", "folder": "/x"}, "library"),
])
def test_a_malformed_request_is_400(graph, payload, fragment):
    response = post(POLL, payload)
    assert response.status_code == 400
    assert fragment in as_json(response)["error"]


def test_a_missing_site_is_a_400_not_a_500(graph):
    """The site used to be an app setting, and its absence a RuntimeError -- which
    surfaced as a 500, telling the caller the server was broken when in fact their
    request was. It is request input now, so it is answered as what it is."""
    for missing in ("sharepointHostname", "sharepointSitePath"):
        payload = {k: v for k, v in BODY.items() if k != missing}
        response = post(POLL, payload)

        assert response.status_code == 400, missing
        assert missing in as_json(response)["error"]


def test_an_empty_site_path_means_the_root_site(graph, frozen_now):
    """Exactly the shape `folder` has: "" is a real answer, so only an absent key
    is an error. A tenant whose library lives on the root site must not have to
    invent a path."""
    pending_item(graph)
    graph.add_job("1825", state="processing", created=iso(minutes=1))

    assert post(POLL, body(sharepointSitePath="")).status_code == 200


def test_a_bad_give_up_threshold_is_400(graph):
    assert post(POLL, body(giveUpDays=0)).status_code == 400
    assert post(POLL, body(giveUpDays=9999)).status_code == 400


def test_a_400_never_touches_the_queue(graph, frozen_now):
    pending_item(graph)
    graph.add_job("1825", state="completed")

    post(POLL, {"library": "Documents"})

    assert graph.status_of("1") == print_policy.PENDING
    assert not [c for c in graph.calls if c.method == "PATCH"]


# --- telemetry ----------------------------------------------------------------


def test_a_completion_emits_a_print_event(graph, frozen_now, caplog):
    caplog.set_level(logging.INFO)
    pending_item(graph, name="statement.pdf")
    graph.add_job("1825", state="completed")

    post(POLL, body())

    event = print_events(caplog)[0]
    assert event["ep"] == "poll"
    assert event["result"] == "completed"
    assert event["to"] == "PRINT_COMPLETED"
    assert event["job"] == "1825"
    assert event["file"] == "statement.pdf"


def test_an_in_flight_job_emits_no_print_event(graph, frozen_now, caplog):
    """PRINT_EVENT means "something changed". Emitting one for a job that is
    merely still running would inflate every count in the weekly report."""
    caplog.set_level(logging.INFO)
    pending_item(graph)
    graph.add_job("1825", state="processing")

    post(POLL, body())

    assert print_events(caplog) == []


# --- the cancel-first guard ---------------------------------------------------
# Ported in intent from test_resubmit.py when the Resubmit endpoint was retired.
# The defect these pin (D1) is a property of REPLACING A LIVE JOB, not of which
# endpoint does the replacing, so it followed the behaviour here.


def test_a_stalled_job_is_cancelled_before_the_requeue(graph, frozen_now):
    """THE double-print guard.

    Graph defines `stopped` as "an issue with the printer needs to be addressed
    BEFORE THE JOB CAN CONTINUE" -- the job is alive. Requeue without cancelling
    and, when someone clears the paper jam, the original AND the replacement both
    come out.
    """
    stalled(graph, job_id="1825")

    payload = as_json(post(POLL, body()))

    assert graph.cancelled == ["1825"], "the stalled job must be cancelled"
    assert payload["requeued"] == 1

    cancel_index = next(i for i, c in enumerate(graph.calls) if "/cancel" in c.url)
    patch_index = next(i for i, c in enumerate(graph.calls)
                       if c.method == "PATCH" and "/items/1/fields" in c.url)
    assert cancel_index < patch_index, \
        "cancel must precede handing the file back to Submit"


def test_the_cancel_uses_the_printer_id_not_the_share_id(graph, frozen_now):
    """Cancel is documented only on /print/printers/{id}/..., so it needs the
    PRINTER id while everything else uses the SHARE id."""
    stalled(graph, job_id="1825")

    post(POLL, body())

    cancel_url = graph.calls_to("/cancel", method="POST")[0].url
    assert "/print/printers/{}/jobs/1825/cancel".format(graph.PRINTER_ID) in cancel_url


def test_the_cancel_uses_the_printer_named_on_the_row(graph, frozen_now):
    """Carried over from defect F3. Resubmit could be handed a printerShareId
    that overrode the file's own Printer_Name, so the cancel went to the wrong
    printer, 404'd, read as "already gone", and the original stayed alive to
    print beside its replacement.

    Poll can be GIVEN a printer again (see the printerShareId section below), so
    the two can diverge once more -- but only when one is supplied. This test
    sends none, which is the default every existing flow uses, and pins that with
    more than one share registered the cancel still follows the ROW rather than
    whichever share happens to be first.
    """
    other = graph.add_share("other-share", printer_id="other-printer")
    stalled(graph, printer=other["id"])

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1
    cancels = graph.calls_to("/cancel", method="POST")
    assert cancels, "the stalled job was never cancelled"
    assert "other-printer" in cancels[0].url, \
        "cancel went to the wrong printer: {}".format(cancels[0].url)


@pytest.mark.parametrize("state", ["paused", "unknown", "stopped"])
def test_every_non_terminal_stalled_state_is_cancelled(graph, frozen_now, state):
    """None of these is documented as dead, so all of them may still print.

    `pending` is deliberately absent -- see the R24 tests below."""
    stalled(graph, state=state)

    post(POLL, body())

    assert graph.cancelled == ["1825"]


# --- a job the printer has NOT taken is not stalled (R24) ---------------------


def test_a_pending_job_past_the_threshold_is_not_cancelled(graph, frozen_now):
    """THE BUG THIS CHANGE FIXES. `pending` means the printer has not started the
    job. Cancelling it kills a healthy document waiting its turn, and if the
    device acquired it between the read and the cancel, the original and the
    replacement both print."""
    stalled(graph, state="pending", job_minutes_old=60)

    payload = as_json(post(POLL, body()))

    assert graph.cancelled == [], "a queued job must never be cancelled"
    assert payload["requeued"] == 0
    assert payload["stillRunning"] == 1
    assert graph.field("1", "Print_Status") == print_policy.PENDING
    assert graph.field("1", "Print_JobId") == "1825", \
        "the job id is the audit trail; a row left alone keeps it"
    assert not graph.calls_to("/items/1/fields", method="PATCH")


def test_a_pending_job_past_the_give_up_deadline_is_cancelled_then_failed(
        graph, frozen_now):
    """The exemption is bounded. Past `giveUpDays` the outstanding job is still
    cancelled FIRST -- otherwise it could print days later against a row that
    already reads PRINT_FAILED."""
    stalled(graph, state="pending", file_minutes_old=11 * DAY,
            job_minutes_old=60)

    payload = as_json(post(POLL, body()))

    assert payload["gaveUp"] == 1
    assert graph.cancelled == ["1825"], "an abandoned job must not outlive the row"
    assert graph.field("1", "Print_Status") == print_policy.FAILED


def test_a_job_the_printer_only_just_took_is_not_stalled(graph, frozen_now):
    """The stall clock is how long the PRINTER has held the job, not how long ago
    we created it. A job queued for three hours and acknowledged a minute ago has
    been printing for a minute -- measuring from creation would cancel it on the
    first poll after it finally started."""
    stalled(graph, state="processing", job_minutes_old=180, ack_minutes_old=1)

    payload = as_json(post(POLL, body()))

    assert graph.cancelled == []
    assert payload["stillRunning"] == 1
    assert graph.field("1", "Print_Status") == print_policy.PENDING


def test_a_job_the_printer_has_held_too_long_is_still_cancelled(graph, frozen_now):
    """The other half: an acknowledgement does not make a job immortal."""
    stalled(graph, state="processing", job_minutes_old=180, ack_minutes_old=30)

    payload = as_json(post(POLL, body()))

    assert graph.cancelled == ["1825"]
    assert payload["requeued"] == 1


def test_the_reported_age_is_the_clock_the_decision_was_made_on(graph, frozen_now):
    """The response must not describe a row against a different clock from the one
    that judged it. A message reading `job 180.0 min old (stall 5)` beside a row
    Poll deliberately left alone reads as a bug in Poll, and the next person spends
    an afternoon on it."""
    stalled(graph, state="processing", job_minutes_old=180, ack_minutes_old=1)

    payload = as_json(post(POLL, body()))

    message = result_for(payload, "1")["message"]
    assert "1.0 min old" in message, message
    assert "180" not in message, "the creation stamp leaked into the message"


def test_a_failed_cancel_still_requeues(graph, frozen_now):
    """Best-effort by contract: a stuck document is worse than a possible
    duplicate. The cancel failure is logged -- that log line is the only warning
    a second copy may appear."""
    stalled(graph)
    graph.fail_next("POST", "/cancel", status=500)

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1
    assert graph.field("1", "Print_Status") == print_policy.READY


# --- the cancel the row CLAIMS versus the one that happened -------------------
# THE REGRESSION THESE EXIST FOR, seen live on 2026-09-02. Poll wrote "Job Id 38
# cancelled" into Print_Message while the portal showed job 38 as `stopped`. The
# answer was available -- `_cancel_outstanding` returns it -- and both callers
# threw it away. A requeue then clears Print_JobId, so nothing looks at that job
# again and the false claim is the only record left.


def test_a_failed_cancel_is_not_recorded_as_a_cancel(graph, frozen_now):
    """The row still requeues -- that is rule 2 and unchanged -- but the audit
    trail must not assert something nobody checked."""
    stalled(graph)
    graph.fail_next("POST", "/cancel", status=500)

    payload = as_json(post(POLL, body()))

    message = graph.field("1", "Print_Message")
    assert "CANCEL FAILED" in message
    assert "cancelled" not in message, \
        "Print_Message claimed a cancel that did not take"
    assert "duplicate" in result_for(payload, "1")["warning"]


def test_a_cancel_the_device_ignores_is_flagged_as_a_possible_duplicate(
        graph, frozen_now):
    """Graph accepting the cancel is not the job dying. On the live run of
    2026-09-02 one Poll pass cancelled four jobs: 40 and 41 reached `canceled`,
    while 38 and 39 went on reading `stopped`. Poll reported all four as cancelled
    and said nothing about the difference."""
    stalled(graph, state="stopped")
    graph.jobs_held_by_device.append("1825")

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1, "the file must still be requeued"
    warning = result_for(payload, "1")["warning"]
    assert "1825" in warning and "stopped" in warning
    assert "accepted" in warning, "an observation, not a verdict -- cancel is async"


def test_the_verification_read_can_never_fail_the_requeue(graph, frozen_now):
    """THE RE-READ IS A DIAGNOSTIC AND MUST NOT BE LOAD-BEARING.

    It happens AFTER the cancel and BEFORE the PATCH to PRINT_READY. Left
    unguarded, a transient Graph error there unwinds the whole route to a 500 with
    the row still PRINT_PENDING -- and its job now `canceled`, so the NEXT Poll run
    reads a terminal state and writes PRINT_FAILED. A blip on a call that exists
    only to improve a log message would cost the document its print.

    `skip=1` lets Poll's own decision read succeed and fails only the read that
    follows the cancel.
    """
    stalled(graph)
    graph.fail_next("GET", "/jobs/1825", status=500, skip=1)

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1
    assert graph.field("1", "Print_Status") == print_policy.READY
    assert graph.cancelled == ["1825"], "the cancel still happened"


def test_a_clean_cancel_raises_no_warning(graph, frozen_now):
    """The ordinary path must stay silent, or the warning is noise by the time it
    matters."""
    stalled(graph)

    payload = as_json(post(POLL, body()))

    assert graph.cancelled == ["1825"]
    assert "warning" not in result_for(payload, "1")
    assert "cancelled" in graph.field("1", "Print_Message")


def test_giving_up_does_not_claim_a_cancel_that_failed(graph, frozen_now):
    """Worse here than on a requeue: PRINT_FAILED is terminal, nothing follows it,
    and an abandoned job prints days later against a row saying it never did."""
    stalled(graph, file_minutes_old=40 * DAY)
    graph.fail_next("POST", "/cancel", status=500)

    payload = as_json(post(POLL, body()))

    assert payload["gaveUp"] == 1
    message = graph.field("1", "Print_Message")
    assert "NOT CANCELLED" in message and "may still print" in message
    assert "warning" in result_for(payload, "1")


# --- requeue ------------------------------------------------------------------


def test_a_requeued_file_goes_back_to_ready_with_the_job_id_cleared(graph, frozen_now):
    """PRINT_READY hands it to Submit; the job id is cleared because the job it
    named has just been cancelled and no longer exists."""
    stalled(graph)

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1
    assert graph.field("1", "Print_Status") == print_policy.READY
    assert graph.field("1", "Print_JobId") == ""


def test_the_requeue_appends_to_the_message_rather_than_replacing_it(graph, frozen_now):
    """The retry history is the only account a person has of what happened to a
    document, so a requeue adds to it instead of erasing the previous reason."""
    stalled(graph, message="convert: earlier failure")

    post(POLL, body())

    message = graph.field("1", "Print_Message")
    assert "convert: earlier failure" in message, "the earlier entry must survive"
    assert "1825" in message and "Retry job" in message


def test_the_retry_history_survives_the_next_submit(graph, frozen_now):
    """THE REGRESSION THAT MATTERS FOR THIS FEATURE.

    Poll appends the retry note on the way OUT of PRINT_PENDING, and Submit's
    claim is the very next write. While that claim cleared Print_Message, the
    note was erased within one Flow A cycle -- at most fifteen minutes -- so the
    column could never hold more than one entry and nobody would ever see the
    history the requeue exists to record.

    Two cycles through the REAL endpoints, because that is the only way to catch
    it: the requeue test alone passes either way.
    """
    from helpers import SUBMIT

    submit_body = dict(BODY, printerShareId=graph.SHARE_ID)
    stalled(graph, file_minutes_old=40, job_id="1825")

    as_json(post(POLL, body()))
    assert "1825" in graph.field("1", "Print_Message")

    as_json(post(SUBMIT, submit_body))
    assert "1825" in graph.field("1", "Print_Message"),         "the claim erased the retry history"

    second_job = graph.field("1", "Print_JobId")
    graph.add_job(second_job, state="stopped", created=iso(minutes=40))
    graph.items["1"]["lastModifiedDateTime"] = iso(minutes=40)

    as_json(post(POLL, body()))
    message = graph.field("1", "Print_Message")
    assert "1825" in message and second_job in message,         "both attempts must appear: {!r}".format(message)


def test_the_requeue_sends_an_if_match_header(graph, frozen_now):
    """The eTag is what makes two overlapping Poll runs safe. Asserting the
    RESULT of a 412 is not enough -- if the header were never sent, no 412 could
    ever occur and the test would pass vacuously."""
    stalled(graph)
    etag_before = graph.items["1"]["eTag"]   # the write bumps it

    post(POLL, body())

    patch = graph.calls_to("/items/1/fields", method="PATCH")[0]
    assert patch.headers.get("If-Match") == etag_before


def test_a_row_with_a_job_but_no_printer_is_never_given_a_terminal_status(
        graph, frozen_now):
    """A DELIBERATE GAP, pinned so it is not mistaken for an oversight.

    Printer_Name empty means the job cannot be looked up and cannot be cancelled
    -- job ids are per-printer. Writing PRINT_FAILED would be a guess about a
    document that may well have printed, so Poll reports the row as `malformed`
    on EVERY run and leaves the status alone. That is visible, unlike the old G1
    strand which was silent, but it does mean age alone will never resolve it.
    A human must fix the column.
    """
    graph.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer="", created=iso(days=99))

    payload = as_json(post(POLL, body()))

    assert payload["malformed"] == 1
    assert payload["gaveUp"] == 0
    assert graph.field("1", "Print_Status") == print_policy.PENDING
    assert not graph.calls_to("/items/1/fields", method="PATCH")


def test_the_requeue_keeps_the_printer_name(graph, frozen_now):
    """Printer_Name is how the next Submit and any later cancel find the device.
    Clearing it would strand the row."""
    stalled(graph)

    post(POLL, body())

    assert graph.field("1", "Printer_Name") == graph.SHARE_ID


def test_a_crashed_submission_is_requeued(graph, frozen_now):
    """UC-6: Submit claimed the file and died before creating the job, leaving
    PRINT_PENDING with an empty Print_JobId. That is the deliberate cost of
    claiming first (rule 1). Poll now owns the recovery; this used to wait 72h for
    a separate endpoint."""
    graph.add_item("1", status=print_policy.PENDING, job_id="",
                   printer=graph.SHARE_ID, created=iso(days=1))

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1
    assert graph.field("1", "Print_Status") == print_policy.READY
    assert graph.cancelled == [], "there is no job to cancel"
    # ...and the row must not claim one was. This used to read "Job Id (none)
    # cancelled", which asserted a cancel about a job that never existed.
    assert graph.field("1", "Print_Message").startswith("No job to cancel")
    assert "warning" not in result_for(payload, "1"), \
        "a submission that crashed before creating a job cannot duplicate"


def test_a_job_purged_from_universal_print_is_requeued(graph, frozen_now):
    """A 404 means Universal Print no longer has it, so nothing will ever
    complete it. Still counted as notFound for visibility."""
    pending_item(graph, job_id="9999")  # no matching job registered

    payload = as_json(post(POLL, body()))

    assert payload["notFound"] == 1
    assert payload["requeued"] == 1
    assert payload["completed"] == 0
    assert graph.field("1", "Print_Status") == print_policy.READY


def test_a_lost_etag_skips_the_requeue(graph, frozen_now):
    """Two overlapping Poll runs must not both requeue one file, or the message
    gains a duplicate entry and the cancel runs twice."""
    stalled(graph)
    graph.fail_next("PATCH", "/items/1/fields", status=412)

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 0
    assert result_for(payload, "1")["result"] == "skipped"


def test_a_requeue_emits_both_a_cancel_and_a_requeue_event(graph, frozen_now, caplog):
    """Two events, in this order. The cancel line is the ONLY record that a
    particular job was killed -- Print_JobId is cleared by the requeue that
    follows it -- and `cancelled` is part of the closed result vocabulary the
    weekly report counts (design.md §13)."""
    caplog.set_level(logging.INFO)
    stalled(graph, name="statement.pdf")

    post(POLL, body())

    results = [e["result"] for e in print_events(caplog)]
    assert results == ["cancelled", "requeued"], results

    requeue = [e for e in print_events(caplog) if e["result"] == "requeued"][0]
    assert requeue["to"] == print_policy.READY
    assert requeue["job"] == "1825"

    cancel = [e for e in print_events(caplog) if e["result"] == "cancelled"][0]
    assert cancel["job"] == "1825", "the cancel must name the job it killed"


# --- the exponential schedule -------------------------------------------------


def test_a_job_inside_the_stall_threshold_is_left_alone(graph, frozen_now):
    """Four minutes is not stalled at a five-minute threshold. Cancelling here
    would kill a job that is simply queued behind another."""
    stalled(graph, file_minutes_old=5, job_minutes_old=4)

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 0
    assert graph.cancelled == []


def test_a_stalled_job_inside_the_backoff_gap_is_requeued_with_a_future_due_time(
        graph, frozen_now):
    """THE BACKOFF ITSELF, now written into the row instead of held in Poll.

    This file was requeued 27 minutes into its life, so two retries are spent and
    the third is not due until 35 minutes. At 32 minutes the job is stalled but the
    boundary is still three minutes away. Poll used to answer by doing nothing, and
    the row waited at PRINT_PENDING while every run re-derived the same silence.

    Now the row is requeued at once and carries the wait as a due time. The third
    attempt still happens at 35 minutes -- Submit is what holds it until then -- but
    it is a value anyone can read rather than a decision nobody could see.

    The offline printer this guards against is still guarded: the expensive work
    (render, conversion, upload) happens when Submit acts, and Submit will not act
    until the due time passes.
    """
    graph.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer=graph.SHARE_ID, created=iso(minutes=32),
                   modified=iso(minutes=5))
    graph.add_job("1825", state="stopped", created=iso(minutes=5))

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1
    assert graph.status_of("1") == print_policy.READY
    assert graph.cancelled == ["1825"], "the stalled job is killed before the retry"

    due = print_policy.parse_graph_datetime(graph.field("1", "Print_Time"))
    assert due == NOW + timedelta(minutes=3), (
        "retry three falls due 35 minutes after creation, three minutes from now")
    assert result_for(payload, "1")["retry"] == 3


def test_a_stalled_job_past_its_boundary_is_given_a_due_time_in_the_past(
        graph, frozen_now):
    """The same file eight minutes later. The due time is behind us, so the row is
    immediately printable -- which is what reproduces the pre-Print_Time timing for
    every boundary that has already gone by when Poll first looks."""
    graph.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer=graph.SHARE_ID, created=iso(minutes=40),
                   modified=iso(minutes=13))
    graph.add_job("1825", state="stopped", created=iso(minutes=13))

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1
    due = print_policy.parse_graph_datetime(graph.field("1", "Print_Time"))
    assert due == NOW - timedelta(minutes=5), "the 35-minute boundary, five minutes ago"
    assert print_policy.is_due(due, NOW) is True


def test_a_stalled_job_past_the_next_boundary_is_requeued(graph, frozen_now):
    """The same file eight minutes later: 40 minutes old, past the 35-minute
    boundary for retry three, so it fires."""
    graph.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer=graph.SHARE_ID, created=iso(minutes=40),
                   modified=iso(minutes=13))
    graph.add_job("1825", state="stopped", created=iso(minutes=13))

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1


def test_the_first_retry_falls_due_at_the_stall_threshold(graph, frozen_now):
    """Boundary one: base * (2**1 - 1) = 5 minutes."""
    graph.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer=graph.SHARE_ID, created=iso(minutes=6))
    graph.add_job("1825", state="stopped", created=iso(minutes=6))

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1


def test_the_due_time_is_never_scheduled_past_the_give_up_deadline(graph, frozen_now):
    """THE CLAMP, WHICH IS THE ONLY UPPER BOUND LEFT.

    An eight-day-old file whose eleventh retry has already been spent. The twelfth
    falls due at 20475 minutes -- 14.2 days, four past the ten-day deadline -- and a
    row waiting that long sits at PRINT_READY, where Poll (which queries
    PRINT_PENDING only) could not see it to fail it. It would print days late
    against a row about to read PRINT_FAILED.

    Clamped, the last attempt lands ON the deadline instead: it gets stallMinutes
    of life, and the run after that finds the file out of window and fails it.
    """
    stalled(graph, file_minutes_old=8 * DAY, attempt_at_minutes=8 * DAY - 600)

    payload = as_json(post(POLL, body()))

    assert payload["requeued"] == 1, "work continues; there is no grace period now"
    assert result_for(payload, "1")["retry"] == 12

    created = NOW - timedelta(minutes=8 * DAY)
    assert 5 * (2 ** 12 - 1) > 10 * DAY, "retry twelve really is outside the window"
    due = print_policy.parse_graph_datetime(graph.field("1", "Print_Time"))
    assert due == created + timedelta(days=10), "clamped to the deadline exactly"


def test_the_clamped_final_attempt_is_failed_rather_than_stranded(graph, monkeypatch):
    """THE OTHER HALF OF THE CLAMP, and the reason it closes a hole rather than
    moving one.

    Scheduling the last attempt ON the deadline is only safe if something then
    fails the row. Nothing would, if the row were still waiting at PRINT_READY --
    Poll queries PRINT_PENDING only. So: Submit claims it at the deadline, which
    puts it back where Poll can see it, and the next run finds the file out of
    window, cancels the job it just created, and writes PRINT_FAILED.
    """
    from helpers import SUBMIT

    created = NOW - timedelta(days=10)
    clock = {"now": NOW}
    monkeypatch.setattr(print_policy, "now_utc", lambda: clock["now"])

    # Waiting at PRINT_READY with a due time of exactly the deadline -- the row
    # shape the clamp produces.
    graph.add_item("1", status=print_policy.READY,
                   created=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   print_time=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))

    submitted = as_json(post(SUBMIT, dict(BODY, printerShareId=graph.SHARE_ID)))
    assert submitted["submitted"] == 1, "due exactly on the deadline, so claimable"
    assert graph.status_of("1") == print_policy.PENDING

    # The next Poll run, ten minutes later: the file is now outside the window.
    clock["now"] = NOW + timedelta(minutes=10)
    payload = as_json(post(POLL, body()))

    assert payload["gaveUp"] == 1
    assert graph.status_of("1") == print_policy.FAILED
    assert graph.cancelled, "the job created at the deadline must be cancelled"
    assert not graph.field("1", "Print_Time"), "and the schedule cleared with it"


def test_a_job_completing_late_still_counts_as_printed(graph, frozen_now):
    """Completion is checked before the deadline, so a job that finishes while the
    schedule has all but run out is still recorded as printed rather than failed."""
    pending_item(graph, days_old=5)
    graph.add_job("1825", state="completed", created=iso(days=5))

    payload = as_json(post(POLL, body()))

    assert payload["completed"] == 1
    assert graph.field("1", "Print_Status") == print_policy.COMPLETED


# --- giving up ----------------------------------------------------------------


def test_a_file_past_the_give_up_threshold_is_failed(graph, frozen_now):
    """This is what closes G1. A file this old used to be dropped from Poll's
    window and touched by nothing ever again -- no status change, no alert."""
    stalled(graph, file_minutes_old=11 * DAY)

    payload = as_json(post(POLL, body()))

    assert payload["gaveUp"] == 1
    assert graph.field("1", "Print_Status") == print_policy.FAILED
    assert "gave up" in graph.field("1", "Print_Message")


def test_giving_up_cancels_the_outstanding_job(graph, frozen_now):
    """Otherwise the abandoned job prints days later against a row that reads
    PRINT_FAILED -- the column would be lying about paper in the tray."""
    stalled(graph, file_minutes_old=11 * DAY)

    post(POLL, body())

    assert graph.cancelled == ["1825"]


# --- Print_Time is only ever set on a PRINT_READY row -------------------------


def test_a_terminal_outcome_clears_the_due_time(graph, frozen_now):
    """THE INVARIANT, and it is not tidiness. PRINT_FAILED is terminal and the
    documented recovery is that a human sets the row back to PRINT_READY. If a
    future due time survived that reset, Submit would decline to print and the
    reset would appear to do nothing at all -- with no error anywhere to explain
    it.

    Each of these rows carries a due time in the future before the run, and must
    not afterwards.
    """
    # completed
    pending_item(graph, "done", job_id="900", print_time=iso(minutes=-30))
    graph.add_job("900", state="completed")
    # canceled at the printer -- terminal, never retried
    pending_item(graph, "killed", job_id="901", print_time=iso(minutes=-30))
    graph.add_job("901", state="canceled", description="Canceled at the device.")
    # past the give-up deadline
    stalled(graph, "old", job_id="902", file_minutes_old=11 * DAY,
            print_time=iso(minutes=-30))

    payload = as_json(post(POLL, body()))

    assert (payload["completed"], payload["failed"], payload["gaveUp"]) == (1, 1, 1)
    for item_id, status in (("done", print_policy.COMPLETED),
                            ("killed", print_policy.FAILED),
                            ("old", print_policy.FAILED)):
        assert graph.status_of(item_id) == status
        assert not graph.field(item_id, "Print_Time"), (
            "{} kept a due time it can no longer act on".format(item_id))


def test_a_completed_job_past_the_give_up_threshold_is_still_completed(graph, frozen_now):
    """ORDER OF CHECKS. The paper came out. A row saying PRINT_FAILED about a
    document that printed is worse than a late success."""
    pending_item(graph, days_old=11)
    graph.add_job("1825", state="completed", created=iso(days=11))

    payload = as_json(post(POLL, body()))

    assert payload["completed"] == 1
    assert payload["gaveUp"] == 0
    assert graph.field("1", "Print_Status") == print_policy.COMPLETED


def test_the_give_up_message_appends_rather_than_replacing(graph, frozen_now):
    stalled(graph, file_minutes_old=11 * DAY, message="convert: bad PDF")

    post(POLL, body())

    assert "convert: bad PDF" in graph.field("1", "Print_Message")


def test_a_give_up_emits_both_a_cancel_and_a_gave_up_event(graph, frozen_now, caplog):
    caplog.set_level(logging.INFO)
    stalled(graph, file_minutes_old=11 * DAY, name="statement.pdf")

    post(POLL, body())

    results = [e["result"] for e in print_events(caplog)]
    assert results == ["cancelled", "gave_up"], results

    gave_up = [e for e in print_events(caplog) if e["result"] == "gave_up"][0]
    assert gave_up["to"] == print_policy.FAILED


# --- tunables from the flow body ----------------------------------------------


def test_the_stall_threshold_can_be_set_per_request(graph, frozen_now):
    """The whole point of the change: retune the pacing by editing a Power
    Automate flow, with no deploy and no app-setting edit."""
    # A three-minute-old file whose job has sat for two. Nothing is stalled at the
    # five-minute default, so the first call is a genuine no-op and leaves the row
    # untouched for the second.
    stalled(graph, file_minutes_old=3, job_minutes_old=2)

    assert as_json(post(POLL, body()))["requeued"] == 0, "2 min < the 5 min default"
    assert as_json(post(POLL, body(stallMinutes=1)))["requeued"] == 1


def test_the_give_up_threshold_can_be_set_per_request(graph, frozen_now):
    # The job is fresh, so the first call requeues nothing and the row survives
    # unchanged into the second -- giving up is checked BEFORE stalling, so a
    # healthy job past the deadline is still abandoned.
    stalled(graph, file_minutes_old=5 * DAY, job_minutes_old=1)

    assert as_json(post(POLL, body()))["gaveUp"] == 0, "5 days < the 10 day default"
    assert as_json(post(POLL, body(giveUpDays=4)))["gaveUp"] == 1


@pytest.mark.parametrize("payload", [
    {"stallMinutes": 0}, {"stallMinutes": 99999},
    {"giveUpDays": 0}, {"giveUpDays": 400},
])
def test_an_out_of_range_tunable_is_400_and_touches_nothing(graph, frozen_now, payload):
    """An out-of-range REQUEST value is the caller's error, not a server fault,
    and it must be refused before anything is written."""
    stalled(graph)

    response = post(POLL, body(**payload))

    assert response.status_code == 400
    assert not graph.calls_to("/items/1/fields", method="PATCH")
    assert graph.cancelled == []


def test_the_response_echoes_the_settings_in_force(graph, frozen_now):
    """So a flow's own tuning is visible in the response and the App Insights
    trace, not just in whatever the flow meant to send."""
    pending_item(graph)
    graph.add_job("1825", state="processing", created=iso(minutes=1))

    payload = as_json(post(POLL, body(stallMinutes=7, giveUpDays=3)))

    assert payload["stallMinutes"] == 7
    assert payload["giveUpDays"] == 3
    assert "maxRetries" not in payload, "the second bound is gone, not renamed"


def test_a_flow_still_sending_max_retries_is_not_rejected(graph, frozen_now):
    """`maxRetries` was retired, but every deployed Flow B body still carries it.
    Accepting and ignoring an unknown key is what lets the code deploy before the
    flows are edited; 400-ing would take the pipeline down at the moment of the
    deploy instead."""
    pending_item(graph)
    graph.add_job("1825", state="processing", created=iso(minutes=1))

    response = post(POLL, body(maxRetries=4))

    assert response.status_code == 200
    assert "maxRetries" not in as_json(response)


# --- printerShareId: the request-level printer override -----------------------
#
# Poll deliberately took NO printer for most of its life. That was the structural
# fix for defect F3: Resubmit accepted a printerShareId that could override the
# file's own Printer_Name, so a cancel went to the wrong printer, 404'd, read as
# "already gone", and the original job stayed alive to print beside its
# replacement.
#
# The parameter is back, by explicit decision, as a HARD OVERRIDE -- and with it
# F3's precondition. The tests below pin what that means, including the exposure,
# because a reintroduced hazard that nothing describes is how it gets forgotten.
#
# WHAT THESE TESTS CANNOT SHOW. Job ids are per-printer in the real service, so
# looking up a job on the wrong printer 404s. FakeGraph keys jobs GLOBALLY, so it
# cannot reproduce that 404 and therefore cannot demonstrate the double print
# itself. What is pinned here is everything upstream of it: which printer each
# call is addressed to, and that a divergence is counted and logged.


def test_without_an_override_each_row_follows_its_own_printer(graph, frozen_now):
    """The default, and the behaviour every existing flow depends on."""
    other = graph.add_share("other-share", printer_id="other-printer")
    stalled(graph, printer=other["id"])

    payload = as_json(post(POLL, body()))

    assert payload["printerShareId"] is None
    assert payload["printerOverridden"] == 0
    assert "other-printer" in graph.calls_to("/cancel", method="POST")[0].url


def test_an_override_redirects_the_job_lookup(graph, frozen_now):
    """Hard override: the row says one share, the request says another, and the
    request wins. This is the behaviour that was asked for."""
    graph.add_share("other-share", printer_id="other-printer")
    stalled(graph, printer="other-share")

    payload = as_json(post(POLL, body(printerShareId=graph.SHARE_ID)))

    assert payload["printerShareId"] == graph.SHARE_ID
    lookup = graph.calls_to("/jobs/1825", method="GET")[0]
    assert "/print/shares/{}/jobs/1825".format(graph.SHARE_ID) in lookup.url


def test_an_override_redirects_the_cancel(graph, frozen_now):
    """And the cancel with it -- the two must not disagree, or Poll would read a
    job on one printer and cancel it on another."""
    graph.add_share("other-share", printer_id="other-printer")
    stalled(graph, printer="other-share")

    payload = as_json(post(POLL, body(printerShareId=graph.SHARE_ID)))

    assert payload["requeued"] == 1
    cancel = graph.calls_to("/cancel", method="POST")[0]
    assert "/print/printers/{}/jobs/1825/cancel".format(graph.PRINTER_ID) in cancel.url


def test_a_divergent_row_is_counted_and_warned_about(graph, frozen_now, caplog):
    """THE EXPOSURE, MADE VISIBLE. A row naming a different printer from the
    override is the one shape that can print twice. It is not refused -- the
    override was asked for -- but it is counted and logged, because in the real
    service that job cannot be cancelled from the overriding printer."""
    graph.add_share("other-share", printer_id="other-printer")
    stalled(graph, printer="other-share")

    with caplog.at_level(logging.WARNING):
        payload = as_json(post(POLL, body(printerShareId=graph.SHARE_ID)))

    assert payload["printerOverridden"] == 1
    warnings = [r.getMessage() for r in caplog.records
                if "printerShareId override" in r.getMessage()]
    assert len(warnings) == 1
    assert "F3" in warnings[0], "the warning must name the defect it re-opens"


def test_an_override_matching_the_row_is_not_a_divergence(graph, frozen_now):
    """The single-printer deployment, which is the intended use. The override
    equals Printer_Name on every row, nothing diverges, and the exposure counter
    stays at zero -- which is what makes it worth reading."""
    stalled(graph, "1")
    stalled(graph, "2", job_id="1826")

    payload = as_json(post(POLL, body(printerShareId=graph.SHARE_ID)))

    assert payload["requeued"] == 2
    assert payload["printerOverridden"] == 0


def test_an_override_rescues_a_row_with_no_printer_name(graph, frozen_now):
    """Defect G3: a row carrying a Print_JobId but an empty Printer_Name could be
    neither read nor cancelled, so it was reported `malformed` on every run and
    never reached a terminal status. An override supplies the missing printer and
    the row rejoins the schedule."""
    graph.add_item("1", status=print_policy.PENDING, job_id="1825", printer="",
                   created=iso(days=1), modified=iso(days=1))
    graph.add_job("1825", state="stopped", created=iso(days=1))

    payload = as_json(post(POLL, body(printerShareId=graph.SHARE_ID)))

    assert payload["malformed"] == 0, "the override should have supplied the printer"
    assert payload["requeued"] == 1
    assert graph.status_of("1") == print_policy.READY
    # Not a divergence: the row named nothing to disagree with.
    assert payload["printerOverridden"] == 0


def test_without_an_override_a_row_with_no_printer_name_is_still_malformed(graph,
                                                                          frozen_now):
    """G3 unchanged when no override is sent -- the rescue is opt-in, not a
    silent behaviour change for existing flows."""
    graph.add_item("1", status=print_policy.PENDING, job_id="1825", printer="",
                   created=iso(days=1), modified=iso(days=1))

    payload = as_json(post(POLL, body()))

    assert payload["malformed"] == 1
    assert graph.status_of("1") == print_policy.PENDING


@pytest.mark.parametrize("supplied", [123, [], {}])
def test_a_non_string_printer_share_id_is_400(graph, frozen_now, supplied):
    stalled(graph)

    response = post(POLL, body(printerShareId=supplied))

    assert response.status_code == 400
    assert graph.cancelled == []


@pytest.mark.parametrize("supplied", [None, "", "   "])
def test_an_absent_or_blank_override_leaves_the_row_in_charge(graph, frozen_now,
                                                              supplied):
    other = graph.add_share("other-share", printer_id="other-printer")
    stalled(graph, printer=other["id"])

    payload = as_json(post(POLL, body(printerShareId=supplied)))

    assert payload["printerShareId"] is None
    assert "other-printer" in graph.calls_to("/cancel", method="POST")[0].url


def test_the_override_is_reported_on_the_run_summary(graph, frozen_now, caplog):
    """RUN_SUMMARY carries printer= for Submit; Poll left it "-" because it had
    no printer. With an override there is one, and the weekly report should be
    able to tell the two kinds of run apart."""
    stalled(graph)

    with caplog.at_level(logging.INFO):
        post(POLL, body(printerShareId=graph.SHARE_ID))

    summary = [s for s in run_summaries(caplog) if s["ep"] == "poll"][0]
    assert summary["printer"] == graph.SHARE_ID


def test_a_divergent_row_with_no_job_is_counted_but_not_called_a_duplicate_risk(
        graph, frozen_now, caplog):
    """The count is CONFIGURATION divergence; the F3 warning is DUPLICATE risk.

    A crashed submission -- PRINT_PENDING with no Print_JobId -- on a row naming
    another printer still means the flow and the library disagree, so it counts.
    But there is no outstanding job, so nothing can print twice, and telling
    somebody it might would be false. A warning that cries wolf is one nobody
    reads by the time it matters.
    """
    graph.add_share("other-share", printer_id="other-printer")
    graph.add_item("1", status=print_policy.PENDING, job_id="",
                   printer="other-share", created=iso(days=1), modified=iso(days=1))

    with caplog.at_level(logging.WARNING):
        payload = as_json(post(POLL, body(printerShareId=graph.SHARE_ID)))

    assert payload["printerOverridden"] == 1, "the disagreement must still count"

    warnings = [r.getMessage() for r in caplog.records
                if "printerShareId override" in r.getMessage()]
    assert len(warnings) == 1
    assert "F3" not in warnings[0], "no job means no duplicate to warn about"
    assert "nothing can print twice" in warnings[0]
    # It is still recovered, exactly as it would be with no override at all.
    assert payload["requeued"] == 1
    assert graph.cancelled == [], "there was no job to cancel"
