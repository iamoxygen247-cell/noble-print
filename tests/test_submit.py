"""
test_submit.py — Tier C: the Submit endpoint, end to end, offline.

The specification under test is the write matrix in docs/design.md §5.4: one
assertion per row. The single most important test in this file is
test_the_claim_precedes_the_print_job -- the ordering it pins is what makes a
crash lose a print (recoverable) instead of printing twice (not recoverable),
and nothing else in the suite would notice if it were reversed.
"""

from __future__ import annotations

import logging

import pytest

import print_policy
import universal_print
from helpers import (POLL, SUBMIT, as_json, post, print_events, result_for,
                     run_summaries)

BODY = {"library": "Documents", "folder": "/Invoices/ToPrint",
        "printerShareId": "share-guid"}


def body(**overrides):
    merged = dict(BODY)
    merged.update(overrides)
    return merged


# --- happy path ---------------------------------------------------------------


def test_submits_a_ready_file_and_records_the_job(graph):
    graph.add_item("1", status="PRINT_READY", name="invoice.pdf")

    response = post(SUBMIT, body())
    payload = as_json(response)

    assert response.status_code == 200
    assert payload["submitted"] == 1
    assert payload["failed"] == 0
    assert payload["skipped"] == 0

    # The write matrix, success row.
    assert graph.field("1", "Print_Status") == print_policy.PENDING
    assert graph.field("1", "Printer_Name") == graph.SHARE_ID
    assert graph.field("1", "Print_JobId") == result_for(payload, "1")["jobId"]
    assert graph.field("1", "Print_Message") == ""


def test_the_claim_precedes_the_print_job(graph):
    """THE ordering guarantee.

    Claim first: a crash loses the print, and Poll requeues it within minutes.
    Claim last: a crash prints the document twice, and nothing recovers that.
    A refactor that "tidies up" by writing the status at the end would pass every
    other test in this file.
    """
    graph.add_item("1")
    post(SUBMIT, body())

    claim_index = next(i for i, c in enumerate(graph.calls)
                       if c.method == "PATCH" and "/items/1/fields" in c.url)
    create_index = next(i for i, c in enumerate(graph.calls)
                        if c.method == "POST" and c.url.endswith("/jobs"))

    assert claim_index < create_index, "the file must be claimed before the job is created"


def test_the_claim_is_etag_conditioned(graph):
    """Without If-Match the claim is not atomic and two overlapping runs both
    proceed -- the double-print this design exists to prevent."""
    item = graph.add_item("1")
    expected_etag = item["eTag"]

    post(SUBMIT, body())

    claim = graph.calls_to("/items/1/fields", method="PATCH")[0]
    assert claim.headers.get("If-Match") == expected_etag


def test_the_claim_clears_the_stale_job_id_but_keeps_the_message(graph):
    """Two halves that used to move together, and only one of them should.

    The JOB ID must go: a stale id would send Poll looking up a job belonging to
    a previous attempt.

    The MESSAGE must stay. Poll writes the retry history on the way OUT of
    PRINT_PENDING and this claim is the very next write, so clearing here erased
    it within one Flow A cycle -- the append could never accumulate and a person
    would essentially never see it. Nothing is lost by keeping it: every terminal
    outcome replaces this column, so a stale entry is visible only while the row
    is PRINT_PENDING, which is when it is worth reading.
    """
    graph.add_item("1", status="PRINT_READY", job_id="OLD-999",
                   message="Job Id OLD-999 cancelled. Retry job (2)")
    post(SUBMIT, body())

    assert graph.field("1", "Print_JobId") != "OLD-999"
    assert graph.field("1", "Print_Message") == \
        "Job Id OLD-999 cancelled. Retry job (2)"


def test_a_completion_replaces_the_retry_history(graph):
    """The safety net that makes keeping the message safe: a terminal outcome
    overwrites the column, so history never outlives the attempt it describes."""
    graph.add_item("1", status="PRINT_READY", message="Job Id 7 cancelled. Retry job (1)")

    post(SUBMIT, body())
    assert "Retry job" in graph.field("1", "Print_Message")

    graph.add_job(graph.field("1", "Print_JobId"), state="completed")
    post(POLL, {"library": "Documents", "folder": "/Invoices/ToPrint"})

    assert graph.field("1", "Print_Message").startswith("printed on ")
    assert "Retry job" not in graph.field("1", "Print_Message")


# --- selection ----------------------------------------------------------------


def test_only_ready_files_are_considered(graph):
    graph.add_item("ready", status="PRINT_READY")
    graph.add_item("pending", status="PRINT_PENDING")
    graph.add_item("done", status="PRINT_COMPLETED")
    graph.add_item("failed", status="PRINT_FAILED")

    payload = as_json(post(SUBMIT, body()))

    assert payload["candidatesFound"] == 1
    assert [i["itemId"] for i in payload["items"]] == ["ready"]
    assert graph.field("pending", "Print_Status") == "PRINT_PENDING"
    assert graph.field("done", "Print_Status") == "PRINT_COMPLETED"


def test_takes_the_oldest_first(graph):
    graph.add_item("newest", created="2026-08-20T10:00:00Z")
    graph.add_item("oldest", created="2026-08-01T10:00:00Z")
    graph.add_item("middle", created="2026-08-10T10:00:00Z")

    payload = as_json(post(SUBMIT, body(batchSize=2)))

    assert [i["itemId"] for i in payload["items"]] == ["oldest", "middle"]


def test_only_files_in_the_requested_folder(graph):
    graph.add_item("in", folder="/sites/Ops/Shared Documents/Invoices/ToPrint")
    graph.add_item("out", folder="/sites/Ops/Shared Documents/Payroll/ToPrint")

    payload = as_json(post(SUBMIT, body()))

    assert [i["itemId"] for i in payload["items"]] == ["in"]


def test_batch_size_defaults_to_five(graph):
    for i in range(9):
        graph.add_item(str(i), created="2026-08-0{}T10:00:00Z".format(i + 1))

    payload = as_json(post(SUBMIT, body()))

    assert payload["submitted"] == 5
    assert payload["candidatesFound"] == 9


def test_batch_size_fifteen_reproduces_the_literal_requirement(graph):
    """The requirement says "the oldest 15". The default is 5 with the flow
    looping, but 15 must remain reachable."""
    for i in range(20):
        graph.add_item(str(i), created="2026-08-{:02d}T10:00:00Z".format(i + 1))

    payload = as_json(post(SUBMIT, body(batchSize=15)))

    assert payload["submitted"] == 15


def test_remaining_ready_drives_the_flow_loop_to_zero(graph):
    """Power Automate loops while remainingReady > 0. If this ever overcounts,
    the Do-Until never terminates and the flow runs until its own timeout."""
    for i in range(12):
        graph.add_item(str(i), created="2026-08-{:02d}T10:00:00Z".format(i + 1))

    seen = []
    for _ in range(5):
        payload = as_json(post(SUBMIT, body()))
        seen.append(payload["remainingReady"])
        if payload["remainingReady"] == 0:
            break

    assert seen == [7, 2, 0], "expected 12 -> 7 -> 2 -> 0 across three batches"


# --- concurrency --------------------------------------------------------------


def test_a_lost_claim_is_skipped_and_no_job_is_created(graph):
    """Two overlapping runs pick the same file; the loser's 412 must stop it
    before it creates a second print job."""
    graph.add_item("1")

    # Simulate the other run winning the race between our read and our claim.
    original_patch = graph._patch_fields
    state = {"bumped": False}

    def bump_then_patch(rel, query, body_, headers):
        if not state["bumped"] and "/items/1/fields" in rel:
            state["bumped"] = True
            graph.items["1"]["version"] += 1
            graph.items["1"]["eTag"] = '"1,99"'
        return original_patch(rel, query, body_, headers)

    graph._patch_fields = bump_then_patch

    payload = as_json(post(SUBMIT, body()))

    assert payload["skipped"] == 1
    assert payload["submitted"] == 0
    assert result_for(payload, "1")["result"] == "skipped"
    assert not [c for c in graph.calls
                if c.method == "POST" and c.url.endswith("/jobs")], \
        "a file we did not claim must never reach the printer"


# --- failure handling ---------------------------------------------------------


@pytest.mark.parametrize("method,fragment,stage", [
    ("GET", "/driveItem", "download"),
    ("POST", "/jobs", universal_print.STAGE_CREATE),
    ("POST", "createUploadSession", universal_print.STAGE_UPLOAD_SESSION),
])
def test_a_failure_at_any_stage_lands_print_failed(graph, method, fragment, stage):
    """The write matrix, failure row: status, printer and a non-empty message --
    and crucially NO job id, because there is no job to poll."""
    graph.add_item("1")
    graph.fail_next(method, fragment, status=500, times=3)

    payload = as_json(post(SUBMIT, body()))

    assert payload["failed"] == 1
    assert graph.field("1", "Print_Status") == print_policy.FAILED
    assert graph.field("1", "Printer_Name") == graph.SHARE_ID
    assert graph.field("1", "Print_JobId") == ""
    message = graph.field("1", "Print_Message")
    assert message, "a PRINT_FAILED row with an empty message is undiagnosable"
    assert message.startswith(stage + ":"), message


def test_a_failed_upload_lands_print_failed(graph):
    graph.add_item("1")
    graph.anon.put_status_override = 500

    payload = as_json(post(SUBMIT, body()))

    assert payload["failed"] == 1
    assert graph.field("1", "Print_Status") == print_policy.FAILED
    assert graph.field("1", "Print_Message").startswith("upload:")


def test_a_failure_on_one_file_does_not_stop_the_batch(graph):
    graph.add_item("bad", created="2026-08-01T10:00:00Z")
    graph.add_item("good", created="2026-08-02T10:00:00Z")
    graph.fail_next("GET", "/items/bad/driveItem", status=500, times=3)

    payload = as_json(post(SUBMIT, body()))

    assert payload["failed"] == 1
    assert payload["submitted"] == 1
    assert graph.field("good", "Print_Status") == print_policy.PENDING


def test_an_unsupported_content_type_fails_before_the_job_is_created(graph):
    """Caught at the capability check rather than as an opaque upload error."""
    graph.share["capabilities"]["contentTypes"] = ["application/oxps"]
    graph.add_item("1", name="invoice.pdf")

    payload = as_json(post(SUBMIT, body()))

    assert payload["failed"] == 1
    assert "does not accept" in graph.field("1", "Print_Message")
    assert not [c for c in graph.calls
                if c.method == "POST" and c.url.endswith("/jobs")]


# --- preflight ----------------------------------------------------------------


def test_an_offline_printer_touches_nothing(graph):
    """The preflight runs before any claim, so an unavailable printer leaves the
    queue exactly as it was and the files retry on the next run."""
    graph.share["isAcceptingJobs"] = False
    graph.add_item("1")

    payload = as_json(post(SUBMIT, body()))

    assert payload["submitted"] == 0
    assert graph.field("1", "Print_Status") == print_policy.READY
    assert not graph.calls_to("/items/1/fields", method="PATCH")
    assert "not accepting jobs" in payload["message"]


def test_an_unknown_printer_share_is_a_server_error(graph):
    graph.add_item("1")
    response = post(SUBMIT, body(printerShareId="no-such-share"))

    assert response.status_code == 500
    assert graph.field("1", "Print_Status") == print_policy.READY


# --- request validation -------------------------------------------------------


@pytest.mark.parametrize("payload,fragment", [
    ({}, "library"),
    ({"library": "Documents"}, "folder"),
    ({"library": "Documents", "folder": "/x"}, "printerShareId"),
    ({"library": "", "folder": "/x", "printerShareId": "s"}, "library"),
])
def test_a_malformed_request_is_400(graph, payload, fragment):
    graph.add_item("1")
    response = post(SUBMIT, payload)

    assert response.status_code == 400
    assert fragment in as_json(response)["error"]


@pytest.mark.parametrize("bad", [0, 16, 99, -1, "abc"])
def test_a_bad_batch_size_is_400(graph, bad):
    graph.add_item("1")
    assert post(SUBMIT, body(batchSize=bad)).status_code == 400


def test_a_400_never_touches_the_queue(graph):
    """A bad request must not claim anything, or its own corrected retry would
    find the files already taken and do nothing."""
    graph.add_item("1")
    post(SUBMIT, {"library": "Documents"})

    assert graph.status_of("1") == print_policy.READY
    assert not [c for c in graph.calls if c.method == "PATCH"]


def test_a_non_json_body_is_400(graph):
    assert post(SUBMIT, b"this is not json").status_code == 400


# --- budget -------------------------------------------------------------------


def test_the_budget_stops_between_files_never_mid_file(graph, monkeypatch):
    """A file is either fully processed or never touched. Stopping mid-file would
    leave a claimed row with no job -- recoverable, but only after 72 hours."""
    monkeypatch.setenv("PRINT_BUDGET_SECONDS", "60")
    for i in range(3):
        graph.add_item(str(i), created="2026-08-0{}T10:00:00Z".format(i + 1))

    # Exhaust the budget after the first file completes.
    import function_app
    real_exhausted = function_app.Budget.exhausted
    calls = {"n": 0}

    def fake_exhausted(self):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(function_app.Budget, "exhausted",
                        property(fake_exhausted))

    payload = as_json(post(SUBMIT, body()))

    assert payload["submitted"] == 1
    assert payload["budgetExhausted"] is True
    # The untouched files are still READY and still claimable next run.
    assert graph.status_of("1") == print_policy.READY
    assert graph.status_of("2") == print_policy.READY


# --- telemetry ----------------------------------------------------------------


def test_emits_one_print_event_per_file(graph, caplog):
    """The weekly reporting in docs/design.md §13 is built entirely on these
    lines. If the field names or order drift, the workbook silently reports
    nothing, so the format is asserted rather than assumed."""
    caplog.set_level(logging.INFO)
    graph.add_item("1", name="invoice.pdf")

    post(SUBMIT, body())

    events = print_events(caplog)
    assert len(events) == 1
    event = events[0]
    assert event["ep"] == "submit"
    assert event["item"] == "1"
    assert event["from"] == "PRINT_READY"
    assert event["to"] == "PRINT_PENDING"
    assert event["result"] == "submitted"
    assert event["printer"] == graph.SHARE_ID
    assert event["job"] != "-"
    assert event["file"] == "invoice.pdf"


def test_a_file_name_with_spaces_still_parses(graph, caplog):
    """file= is deliberately last so a name with spaces cannot break the KQL
    parse of the fields before it."""
    caplog.set_level(logging.INFO)
    graph.add_item("1", name="Invoice 2026-08 Acme Ltd.pdf")

    post(SUBMIT, body())

    event = print_events(caplog)[0]
    assert event["file"] == "Invoice 2026-08 Acme Ltd.pdf"
    assert event["result"] == "submitted"


def test_emits_one_run_summary_per_invocation(graph, caplog):
    caplog.set_level(logging.INFO)
    graph.add_item("1")

    post(SUBMIT, body())

    summaries = run_summaries(caplog)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["ep"] == "submit"
    assert summary["httpStatus"] == "200"
    assert summary["ok"] == "1"
    assert summary["failed"] == "0"


def test_run_summary_uses_sentinels_not_missing_keys(graph, caplog):
    """A missing key breaks a KQL parse; a sentinel is a countable fact."""
    caplog.set_level(logging.INFO)
    post(SUBMIT, {"library": "Documents"})  # 400 path

    summary = run_summaries(caplog)[0]
    assert summary["httpStatus"] == "400"
    for key in ("found", "ok", "failed", "skipped", "remaining"):
        assert key in summary
        assert summary[key] == "-1"


def test_the_created_job_body_carries_only_copies(graph):
    """What actually goes on the wire, not just the constant.

    Relocated here when the retired printer's test file was deleted. The
    companion assertion on the constant itself lives in test_print_policy.py;
    this one proves the constant survives the adapter unchanged.
    """
    graph.add_item("1")

    post(SUBMIT, body())

    created = graph.created_jobs()
    assert len(created) == 1
    assert created[0].body == {"configuration": {"copies": 1}}
