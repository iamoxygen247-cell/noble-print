"""
test_requirements.py — conformance, clause by clause.

One test per sentence of the original requirement, named after it, so anyone can
read this file beside the request and see that each line is actually honoured.
Behaviour is asserted against the SharePoint columns and the Graph calls, not
against internal helpers, because the columns are what the requirement talks about.

Where the implementation deliberately departs from the literal text, the test says
so and asserts the agreed behaviour instead. There are four such places, all
recorded in docs/design.md §2:

  R3   "the oldest 15"  -> batch defaults to 5, ceiling 15 (agreed loop model)
  R5   write order      -> status+printer are written BEFORE the job is created
  R6   "error message returned by Universal Print" -> the error from whichever
                           stage failed, tagged with that stage
  R17  "NOT EQUAL to PRINT_COMPLETED" -> the requirement's other wording,
                           "PRINT_PENDING or PRINT_FAILED", is the one in force
"""

from __future__ import annotations

import re

import pytest

import print_policy
from helpers import POLL, SITE, SUBMIT, as_json, iso, post

SUBMIT_BODY = {**SITE, "library": "Documents", "folder": "/Invoices/ToPrint",
               "printerShareId": "share-guid"}
POLL_BODY = {**SITE, "library": "Documents", "folder": "/Invoices/ToPrint"}


# =============================================================================
# "In sharepoint, I created 4 new columns: Print_Status, Print_JobId,
#  Print_Message, Printer_Name"
# =============================================================================


def test_R1_the_named_columns_plus_the_scheduling_one():
    """The four the requirement names, and a fifth the retry schedule needed.

    `Print_Time` is not in the original requirement: it was added when the backoff
    moved out of Poll's head and into the library, so that a file waiting on a
    retry says when the retry is coming. Every name here is a column somebody
    creates in SharePoint by hand, so the tuple is pinned in full."""
    assert print_policy.COLUMN_DISPLAY_NAMES == (
        "Print_Status", "Print_JobId", "Print_Message", "Printer_Name",
        "Print_Time")


def test_R1_the_app_reads_and_writes_only_those_columns(graph):
    """Nothing else in the library is touched."""
    graph.add_item("1")
    post(SUBMIT, SUBMIT_BODY)

    written = set()
    for call in graph.calls_to("/fields", method="PATCH"):
        written.update(call.body or {})

    allowed = {graph.columns[name] for name in print_policy.COLUMN_DISPLAY_NAMES}
    assert written <= allowed, "wrote outside the app's own columns: {}".format(
        written - allowed)


# =============================================================================
# "an endpoint ... to retrieve the oldest 15 files with Print_Status = PRINT_READY
#  from a sharepoint library"
# =============================================================================


def test_R3_selects_only_print_ready_files(graph):
    for status in ("PRINT_READY", "PRINT_PENDING", "PRINT_FAILED",
                   "PRINT_COMPLETED", ""):
        graph.add_item(status or "blank", status=status)

    payload = as_json(post(SUBMIT, SUBMIT_BODY))

    assert [i["itemId"] for i in payload["items"]] == ["PRINT_READY"]


def test_R3_selects_the_oldest_first(graph):
    graph.add_item("newest", created=iso(days=1))
    graph.add_item("oldest", created=iso(days=30))
    graph.add_item("middle", created=iso(days=15))

    payload = as_json(post(SUBMIT, dict(SUBMIT_BODY, batchSize=3)))

    assert [i["itemId"] for i in payload["items"]] == ["oldest", "middle", "newest"]


def test_R3_fifteen_is_the_ceiling_and_is_reachable(graph):
    """DEVIATION (agreed): the batch defaults to 5 with the Power Automate flow
    looping on remainingReady. Fifteen -- the number in the requirement -- remains
    the maximum and is reachable by asking for it."""
    for i in range(25):
        graph.add_item(str(i), created=iso(days=25 - i))

    assert as_json(post(SUBMIT, SUBMIT_BODY))["submitted"] == 5
    assert print_policy.MAX_BATCH_SIZE == 15
    assert post(SUBMIT, dict(SUBMIT_BODY, batchSize=16)).status_code == 400


def test_R3_reads_from_the_named_library(graph):
    graph.add_item("1")
    post(SUBMIT, SUBMIT_BODY)

    assert graph.calls_to("/lists/Documents", method="GET"), \
        "the library named in the request was never resolved"


# =============================================================================
# "The functionapp needs to call the Universal Print service to create a print job"
# =============================================================================


def test_R4_creates_a_universal_print_job(graph):
    graph.add_item("1")
    post(SUBMIT, SUBMIT_BODY)

    assert graph.calls_to("/print/shares/{}/jobs".format(graph.SHARE_ID),
                          method="POST"), "no print job was created"


def test_R4_uploads_the_document_and_starts_the_job(graph):
    """A job created but never started sits paused forever and never prints."""
    graph.add_item("1")
    post(SUBMIT, SUBMIT_BODY)

    assert graph.calls_to("createUploadSession", method="POST")
    assert [c for c in graph.anon.calls if c.method == "PUT"], "nothing uploaded"
    assert graph.calls_to("/start", method="POST"), "the job was never started"


# =============================================================================
# "If print job is successfully created, then update the Print_Status of the file
#  to PRINT_PENDING, Printer_Name to the Printer Id, and Print_JobId with the
#  JobId returned from Universal Printing service"
# =============================================================================


def test_R5_success_writes_all_three_values(graph):
    graph.add_item("1")

    payload = as_json(post(SUBMIT, SUBMIT_BODY))
    job_id = payload["items"][0]["jobId"]

    assert graph.field("1", "Print_Status") == "PRINT_PENDING"
    assert graph.field("1", "Printer_Name") == "share-guid"
    assert graph.field("1", "Print_JobId") == job_id
    assert job_id in graph.jobs, "the job id written is not one Universal Print issued"


def test_R5_deviation_the_status_is_written_before_the_job_is_created(graph):
    """DEVIATION (agreed, docs/design.md R5): the requirement writes the status
    after the job is created. Here the status and printer are written FIRST, as
    an eTag-conditioned claim, and only the job id afterwards.

    The end state is identical. The difference is how a crash behaves: claiming
    first loses a print (Poll requeues it within minutes), claiming last prints
    the document twice and nothing recovers that.
    """
    graph.add_item("1")
    post(SUBMIT, SUBMIT_BODY)

    first_patch = graph.calls_to("/items/1/fields", method="PATCH")[0]
    create = graph.calls_to("/jobs", method="POST")[0]

    assert graph.calls.index(first_patch) < graph.calls.index(create)
    assert first_patch.body[graph.columns["Print_Status"]] == "PRINT_PENDING"
    assert first_patch.body[graph.columns["Printer_Name"]] == "share-guid"


# =============================================================================
# "If the print job was not created successfully, then update print status to
#  PRINT_FAILED, Printer_Name to the Printer Id, and Print_Message to the error
#  message returned by Universal Print service"
# =============================================================================


def test_R6_failure_writes_status_printer_and_message(graph):
    graph.add_item("1")
    graph.fail_next("POST", "/jobs", status=500, times=3)

    post(SUBMIT, SUBMIT_BODY)

    assert graph.field("1", "Print_Status") == "PRINT_FAILED"
    assert graph.field("1", "Printer_Name") == "share-guid"
    assert graph.field("1", "Print_Message") != ""


def test_R6_the_message_carries_the_services_own_error_text(graph):
    graph.add_item("1")
    graph.fail_next("POST", "/jobs", status=500, times=3,
                    payload={"error": {"code": "printerOffline",
                                       "message": "The printer is offline"}})

    post(SUBMIT, SUBMIT_BODY)

    message = graph.field("1", "Print_Message")
    assert "printerOffline" in message or "offline" in message.lower(), message


def test_R6_deviation_the_message_names_the_stage_that_failed(graph):
    """DEVIATION (docs/design.md R6): the requirement says "the error message
    returned by Universal Print service", but a submission can also fail while
    reading the file out of SharePoint. Reporting that as a printer fault would
    send whoever reads the column to the wrong system, so the stage is named."""
    graph.add_item("1")
    graph.fail_next("GET", "/driveItem", status=500, times=3)

    post(SUBMIT, SUBMIT_BODY)

    assert graph.field("1", "Print_Message").startswith("download:")


# =============================================================================
# "This functionapp's input parameter should have sharepoint folder, sharepoint
#  library, and PrinterId as inputs"
# =============================================================================


@pytest.mark.parametrize("missing,body", [
    ("library", {"folder": "/x", "printerShareId": "share-guid"}),
    ("folder", {"library": "Documents", "printerShareId": "share-guid"}),
    ("printerShareId", {"library": "Documents", "folder": "/x"}),
])
def test_R7_all_three_inputs_are_required(graph, missing, body):
    response = post(SUBMIT, body)
    assert response.status_code == 400
    assert missing in as_json(response)["error"]


def test_R7_the_folder_input_actually_scopes_the_query(graph):
    graph.add_item("in", folder="/sites/Ops/Shared Documents/Invoices/ToPrint")
    graph.add_item("out", folder="/sites/Ops/Shared Documents/Elsewhere")

    payload = as_json(post(SUBMIT, SUBMIT_BODY))

    assert [i["itemId"] for i in payload["items"]] == ["in"]


def test_R7_the_printer_input_is_the_printer_used(graph):
    graph.add_item("1")
    post(SUBMIT, SUBMIT_BODY)

    assert graph.calls_to("/print/shares/share-guid/jobs", method="POST")
    assert graph.field("1", "Printer_Name") == "share-guid"


# =============================================================================
# "another endpoint ... to check on the status of each print job ... by querying
#  the sharepoint folder for all files within the last 20 days with
#  PRINT_STATUS = PRINT_PENDING"
# =============================================================================


def test_R9_queries_only_pending_files(graph, frozen_now):
    for status in ("PRINT_READY", "PRINT_PENDING", "PRINT_FAILED", "PRINT_COMPLETED"):
        graph.add_item(status, status=status, job_id="j-" + status,
                       printer=graph.SHARE_ID, created=iso(days=1))
        graph.add_job("j-" + status, state="completed")

    payload = as_json(post(POLL, POLL_BODY))

    assert [i["itemId"] for i in payload["items"]] == ["PRINT_PENDING"]


def test_R9_superseded_no_pending_row_is_excluded_by_age(graph, frozen_now):
    """R9's 20-day window used to decide WHETHER A ROW WAS LOOKED AT, and a row
    outside it was touched by nothing ever again -- the G1 strand.

    The window is now a give-up threshold (10 days by default), and it decides the
    OUTCOME rather than the scope. Every pending row is examined; an old one is
    failed explicitly instead of vanishing. A job that completed is still recorded
    as completed however old it is, because the paper came out.
    """
    graph.add_item("inside", status="PRINT_PENDING", job_id="a",
                   printer=graph.SHARE_ID, created=iso(days=9))
    graph.add_item("outside", status="PRINT_PENDING", job_id="b",
                   printer=graph.SHARE_ID, created=iso(days=21))
    graph.add_job("a", state="completed", created=iso(days=9))
    graph.add_job("b", state="completed", created=iso(days=21))

    post(POLL, POLL_BODY)

    assert graph.status_of("inside") == "PRINT_COMPLETED"
    assert graph.status_of("outside") == "PRINT_COMPLETED",         "an old row is no longer invisible; it is judged like any other"
    assert print_policy.DEFAULT_GIVE_UP_DAYS == 10


def test_R9_checks_ALL_files_in_the_window_not_a_subset(graph, frozen_now):
    """"all files" is literal. An earlier revision capped this at 15 and starved
    newer jobs behind older still-running ones."""
    for i in range(40):
        graph.add_item(str(i), status="PRINT_PENDING", job_id="j{}".format(i),
                       printer=graph.SHARE_ID, created=iso(days=1))
        graph.add_job("j{}".format(i), state="completed")

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["completed"] == 40
    assert payload["uncheckedCount"] == 0


# =============================================================================
# "If the universal print service returns COMPLETED, then update the file
#  PRINT_STATUS to PRINT_COMPLETED and update Print_Message when the file was
#  printed. Here is an example 'printed on 2026-08-01 14:23:23'"
# =============================================================================


def test_R10_completed_becomes_print_completed(graph, frozen_now):
    graph.add_item("1", status="PRINT_PENDING", job_id="1825",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("1825", state="completed")

    post(POLL, POLL_BODY)

    assert graph.status_of("1") == "PRINT_COMPLETED"


def test_R11_the_message_matches_the_example_format(graph, frozen_now):
    """The requirement's own example: "printed on 2026-08-01 14:23:23"."""
    graph.add_item("1", status="PRINT_PENDING", job_id="1825",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("1825", state="completed")

    post(POLL, POLL_BODY)

    assert re.fullmatch(r"printed on \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                        graph.field("1", "Print_Message"))


def test_R10_a_job_not_yet_completed_is_left_alone(graph, frozen_now):
    graph.add_item("1", status="PRINT_PENDING", job_id="1825",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("1825", state="processing")

    post(POLL, POLL_BODY)

    assert graph.status_of("1") == "PRINT_PENDING"
    assert graph.field("1", "Print_Message") == ""


# =============================================================================
# RETIRED: "another endpoint ... to re-submit outstanding print jobs in
#  PRINT_PENDING or PRINT_FAILED status for the past 20 days and that are older
#  than 72 hours from the initial print request"
#
# R12-R17 described the Resubmit endpoint. That endpoint was removed by decision
# on 2026-09-01 and its recovery work folded into Poll, on an exponential
# schedule starting at five minutes instead of a single 72-hour gate. The clauses
# went with it:
#
#   R12  a third endpoint                 -- no third endpoint exists
#   R13  PRINT_PENDING or PRINT_FAILED    -- Poll queries PRINT_PENDING only;
#                                            PRINT_FAILED is now TERMINAL
#   R14  past 20 days                     -- replaced by giveUpDays, default 10,
#                                            and it now FAILS the row instead of
#                                            silently dropping it (closes G1)
#   R15  older than 72 hours              -- replaced by the retry schedule
#   R17  the scope contradiction          -- moot; there is one query
#
# R16 is the exception. Its insight outlived the endpoint and is pinned below.
# =============================================================================


def test_R16_the_retry_schedule_is_measured_from_the_file_creation_time(
        graph, frozen_now):
    """"The initial print request date/time is the same as the file creation
    datetime in sharepoint."

    Still load-bearing, and for the same reason it always was: OUR OWN WRITES
    BUMP lastModifiedDateTime. Drive the schedule from that and every requeue
    resets the file to age zero, the next boundary is never reached, and the
    retries stop dead after the first one. createdDateTime never moves, so the
    boundaries stay fixed for the life of the document.

    The fixture separates the two. A crashed submission -- PRINT_PENDING with no
    job id -- claimed 13 minutes into the file's life and now 40 minutes old:

        from createdDateTime   40 min -> past the 35 min boundary -> retry 3 DUE
        from lastModified      27 min -> still short of 35        -> nothing due

    So a requeue here can only happen if the right timestamp is being read.
    """
    graph.add_item("1", status="PRINT_PENDING", job_id="", printer=graph.SHARE_ID,
                   created=iso(minutes=40),      # the schedule reads THIS
                   modified=iso(minutes=27))     # not this

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["requeued"] == 1, (
        "the schedule must come from createdDateTime, not lastModifiedDateTime")


def test_PRINT_FAILED_is_terminal_now_that_Resubmit_is_gone(graph, frozen_now):
    """The one behaviour R13 guaranteed that nothing replaces. A failed row is
    never picked up again by anything; a human resets it. Recorded as a test so
    the change is visible rather than merely absent."""
    graph.add_item("1", status="PRINT_FAILED", job_id="", printer=graph.SHARE_ID,
                   created=iso(hours=100))

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["checked"] == 0, "Poll must not touch PRINT_FAILED"
    assert graph.status_of("1") == "PRINT_FAILED"


# =============================================================================
# "When a job is PRINT_PENDING and past the stall threshold, calculate the next
#  retry attempt using the existing arithmetic and update Print_Time, so it is
#  clear and visible when the next print will be retried. The status should
#  still be changed to PRINT_READY."
# =============================================================================


def test_a_stalled_job_goes_back_to_ready_AND_records_when_it_will_retry(
        graph, frozen_now):
    """Both halves of the clause. The status change is the old behaviour; the due
    time is what makes the wait legible to someone looking at the library."""
    graph.add_item("1", status="PRINT_PENDING", job_id="1825",
                   printer=graph.SHARE_ID, created=iso(minutes=32),
                   modified=iso(minutes=5))
    graph.add_job("1825", state="stopped", created=iso(minutes=5))

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["requeued"] == 1
    assert graph.status_of("1") == "PRINT_READY"

    due = print_policy.parse_graph_datetime(graph.field("1", "Print_Time"))
    assert due is not None, "the retry time must be recorded, not just implied"
    # "the existing arithmetic": retry three of stallMinutes * (2**n - 1),
    # counted from the file's creation, exactly as the schedule always was.
    assert due == print_policy.next_retry_time(
        print_policy.parse_graph_datetime(graph.items["1"]["createdDateTime"]),
        5, 3, 10)


def test_the_recorded_retry_time_is_readable_local_time(graph, frozen_now):
    """"Clear and visible" is the point of the column, so it is written where the
    people reading it live -- with the offset, so it still round-trips exactly."""
    graph.add_item("1", status="PRINT_PENDING", job_id="1825",
                   printer=graph.SHARE_ID, created=iso(minutes=40),
                   modified=iso(minutes=13))
    graph.add_job("1825", state="stopped", created=iso(minutes=13))

    post(POLL, POLL_BODY)

    written = graph.field("1", "Print_Time")
    assert written.endswith(("-07:00", "-08:00")), written
    assert print_policy.parse_graph_datetime(written) is not None


# =============================================================================
# "When retrieving SharePoint file properties where Print_Status = PRINT_READY
#  and Print_Time <= Now -- so it only processes files that should be printed."
# =============================================================================


def test_submit_processes_only_files_whose_retry_time_has_arrived(graph, frozen_now):
    """The queue is filtered on BOTH conditions. A file whose retry is still in the
    future is left alone; one whose time has come is printed."""
    graph.add_item("due", print_time=iso(minutes=1))
    graph.add_item("waiting", print_time=iso(minutes=-30))

    payload = as_json(post(SUBMIT, SUBMIT_BODY))

    assert [i["itemId"] for i in payload["items"]] == ["due"]
    assert graph.status_of("waiting") == "PRINT_READY"
    assert payload["notYetDue"] == 1


def test_a_file_with_no_retry_time_is_treated_as_ready_to_print(graph, frozen_now):
    """The upstream process sets PRINT_READY and knows nothing about Print_Time.
    If a blank meant "not due", the pipeline would stop printing entirely on the
    day this shipped."""
    graph.add_item("1")

    assert as_json(post(SUBMIT, SUBMIT_BODY))["submitted"] == 1


# =============================================================================
# "Only cancel job when job status in Universal Print is 'processing' and
#  breached the stall threshold. Do not cancel job when its job status is
#  'pending' in Universal Print unless it breaches the GiveUpDays."
#  (R24, agreed 2026-09-07. Scoped with the owner the same day: every
#   non-terminal state EXCEPT `pending` keeps stalling, because `stopped` is a
#   jam and `paused`/`unknown` are not documented as dead either.)
# =============================================================================


def test_R24_a_pending_job_is_not_cancelled_however_long_it_waits(graph, frozen_now):
    """Graph defines `pending` as "the print job is pending processing by the
    printer" -- the device has not taken it, so nothing is stuck and there is
    nothing to cancel."""
    graph.add_item("1", status="PRINT_PENDING", job_id="1825",
                   printer=graph.SHARE_ID, created=iso(minutes=600))
    graph.add_job("1825", state="pending", created=iso(minutes=600))

    payload = as_json(post(POLL, POLL_BODY))

    assert graph.cancelled == []
    assert payload["requeued"] == 0
    assert graph.status_of("1") == "PRINT_PENDING"


def test_R24_a_processing_job_past_the_threshold_is_still_cancelled(graph,
                                                                    frozen_now):
    """The other half of the clause: a job the printer IS working on and has not
    finished is stuck, and the replacement must not print beside it."""
    graph.add_item("1", status="PRINT_PENDING", job_id="1825",
                   printer=graph.SHARE_ID, created=iso(minutes=600))
    graph.add_job("1825", state="processing", created=iso(minutes=600))

    payload = as_json(post(POLL, POLL_BODY))

    assert graph.cancelled == ["1825"]
    assert payload["requeued"] == 1
    assert graph.status_of("1") == "PRINT_READY"


def test_R24_a_pending_job_past_the_give_up_days_is_cancelled_and_failed(
        graph, frozen_now):
    """"unless it breaches the GiveUpDays" -- the exemption is bounded, and the
    give-up path cancels before it writes, so no abandoned job can print days
    later against a row that reads PRINT_FAILED."""
    graph.add_item("1", status="PRINT_PENDING", job_id="1825",
                   printer=graph.SHARE_ID, created=iso(days=11))
    graph.add_job("1825", state="pending", created=iso(days=11))

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["gaveUp"] == 1
    assert graph.cancelled == ["1825"]
    assert graph.status_of("1") == "PRINT_FAILED"
