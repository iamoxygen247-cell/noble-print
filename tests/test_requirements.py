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
from helpers import POLL, RESUBMIT, SUBMIT, as_json, iso, post

SUBMIT_BODY = {"library": "Documents", "folder": "/Invoices/ToPrint",
               "printerShareId": "share-guid"}
POLL_BODY = {"library": "Documents", "folder": "/Invoices/ToPrint"}


# =============================================================================
# "In sharepoint, I created 4 new columns: Print_Status, Print_JobId,
#  Print_Message, Printer_Name"
# =============================================================================


def test_R1_the_four_columns_are_the_ones_named():
    assert print_policy.COLUMN_DISPLAY_NAMES == (
        "Print_Status", "Print_JobId", "Print_Message", "Printer_Name")


def test_R1_the_app_reads_and_writes_only_those_four(graph):
    """Nothing else in the library is touched."""
    graph.add_item("1")
    post(SUBMIT, SUBMIT_BODY)

    written = set()
    for call in graph.calls_to("/fields", method="PATCH"):
        written.update(call.body or {})

    allowed = {graph.columns[name] for name in print_policy.COLUMN_DISPLAY_NAMES}
    assert written <= allowed, "wrote outside the four columns: {}".format(
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
    first loses a print (Resubmit recovers it after 72h), claiming last prints
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


def test_R9_the_window_is_twenty_days(graph, frozen_now):
    graph.add_item("inside", status="PRINT_PENDING", job_id="a",
                   printer=graph.SHARE_ID, created=iso(days=19))
    graph.add_item("outside", status="PRINT_PENDING", job_id="b",
                   printer=graph.SHARE_ID, created=iso(days=21))
    graph.add_job("a", state="completed")
    graph.add_job("b", state="completed")

    post(POLL, POLL_BODY)

    assert graph.status_of("inside") == "PRINT_COMPLETED"
    assert graph.status_of("outside") == "PRINT_PENDING"
    assert print_policy.DEFAULT_WINDOW_DAYS == 20


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
# "another endpoint ... to re-submit outstanding print jobs in PRINT_PENDING or
#  PRINT_FAILED status for the past 20 days and that are older than 72 hours from
#  the initial print request ... the same as the file creation datetime"
# =============================================================================


@pytest.mark.parametrize("status", ["PRINT_PENDING", "PRINT_FAILED"])
def test_R13_both_named_statuses_are_resubmitted(graph, frozen_now, status):
    graph.add_item("1", status=status, job_id="", printer=graph.SHARE_ID,
                   created=iso(hours=100))

    payload = as_json(post(RESUBMIT, SUBMIT_BODY))

    assert payload["resubmitted"] == 1


def test_R17_resolution_ready_and_blank_are_out_of_scope(graph, frozen_now):
    """The requirement gives two different scopes -- "PRINT_PENDING or
    PRINT_FAILED" and "NOT EQUAL to PRINT_COMPLETED" -- which differ on
    PRINT_READY and on a blank status. The first is the one in force
    (docs/design.md R17): PRINT_READY belongs to Submit."""
    graph.add_item("ready", status="PRINT_READY", created=iso(hours=200))
    graph.add_item("blank", status="", created=iso(hours=200))
    graph.add_item("completed", status="PRINT_COMPLETED", created=iso(hours=200))

    payload = as_json(post(RESUBMIT, SUBMIT_BODY))

    assert payload["candidatesFound"] == 0


def test_R14_the_window_is_twenty_days(graph, frozen_now):
    graph.add_item("inside", status="PRINT_FAILED", job_id="",
                   printer=graph.SHARE_ID, created=iso(days=19))
    graph.add_item("outside", status="PRINT_FAILED", job_id="",
                   printer=graph.SHARE_ID, created=iso(days=21))

    payload = as_json(post(RESUBMIT, SUBMIT_BODY))

    assert [i["itemId"] for i in payload["items"]] == ["inside"]


def test_R15_only_files_older_than_72_hours(graph, frozen_now):
    graph.add_item("young", status="PRINT_FAILED", job_id="",
                   printer=graph.SHARE_ID, created=iso(hours=71))
    graph.add_item("old", status="PRINT_FAILED", job_id="",
                   printer=graph.SHARE_ID, created=iso(hours=73))

    payload = as_json(post(RESUBMIT, SUBMIT_BODY))

    assert [i["itemId"] for i in payload["items"]] == ["old"]
    assert print_policy.DEFAULT_MIN_AGE_HOURS == 72


def test_R16_the_age_is_measured_from_the_file_creation_time(graph, frozen_now):
    """"The initial print request date/time is the same as the file creation
    datetime in sharepoint." Not the last-modified time, which our own writes
    bump -- if age were measured on that, a file we just retried would instantly
    look young and never be retried again.
    """
    graph.add_item("1", status="PRINT_FAILED", job_id="", printer=graph.SHARE_ID,
                   created=iso(hours=100),      # eligible by creation time
                   modified=iso(hours=1))       # touched an hour ago

    payload = as_json(post(RESUBMIT, SUBMIT_BODY))

    assert payload["resubmitted"] == 1, (
        "age must come from createdDateTime, not lastModifiedDateTime")


def test_R12_resubmission_creates_a_new_print_job(graph, frozen_now):
    graph.add_item("1", status="PRINT_FAILED", job_id="", printer=graph.SHARE_ID,
                   created=iso(hours=100))

    post(RESUBMIT, SUBMIT_BODY)

    assert graph.calls_to("/jobs", method="POST"), "no replacement job was created"
    assert graph.field("1", "Print_Status") == "PRINT_PENDING"
    assert graph.field("1", "Print_JobId") != ""
