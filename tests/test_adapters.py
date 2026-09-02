"""
test_adapters.py — Tier B: the SharePoint and Universal Print adapters, and the
transport underneath them, running against FakeGraph.

The real code executes here. FakeGraph replaces requests.Session, so URL
construction, the retry loop, paging, eTag handling, error parsing and the
upload protocol are all under test -- not stubbed out.

The most valuable assertions in this file are the negative ones: that the
download GET and the upload PUT carry NO Authorization header, and that the
create-job POST is never retried. Both are documented failure modes that a
reasonable-looking "just use the authenticated client for everything" refactor
would reintroduce silently.
"""

from __future__ import annotations

import urllib.parse

import pytest

import graph_client
import print_policy
import sharepoint
import universal_print
from fake_graph import FakeGraph, FakeResponse
from graph_client import GraphError
from sharepoint import ColumnNotFound


# --- column resolution --------------------------------------------------------


def test_resolves_encoded_internal_column_names(client, graph):
    """SharePoint fixes internal names at creation and encodes an underscore as
    _x005f_, so a column displayed as "Print_Status" can really be
    "Print_x005f_Status". Nothing may hardcode either form."""
    context = sharepoint.resolve_list(client, graph.SITE_ID, "Documents")

    assert context.internal("Print_Status") == "Print_x005f_Status"
    assert context.internal("Print_JobId") == "Print_x005f_JobId"
    assert context.list_id == graph.LIST_ID


def test_resolves_plain_internal_names_too(monkeypatch, client):
    """A library whose columns were created through the API keeps clean names.
    Both shapes must work, because we cannot know which we will meet."""
    plain = FakeGraph(encoded_columns=False)
    plain.add_item("1")
    context = sharepoint.resolve_list(
        graph_client.GraphClient(lambda: "t", session=plain),
        plain.SITE_ID, "Documents")
    assert context.internal("Print_Status") == "Print_Status"


def test_a_missing_column_fails_loudly_and_names_it(client, graph):
    """Continuing without the status column would print every file and record
    nothing, so they would all print again on the next run. Fatal on purpose."""
    graph.drop_columns = ["Print_Message"]
    with pytest.raises(ColumnNotFound) as excinfo:
        sharepoint.resolve_list(client, graph.SITE_ID, "Documents")
    assert "Print_Message" in str(excinfo.value)


def test_resolve_list_uses_one_call_for_list_and_columns(client, graph):
    """List metadata and column definitions come back together; a second round
    trip per invocation would be pure waste."""
    sharepoint.resolve_list(client, graph.SITE_ID, "Documents")
    list_calls = graph.calls_to("/lists/Documents", method="GET")
    assert len(list_calls) == 1
    assert "expand=columns" in list_calls[0].url.replace("$", "")


# --- querying -----------------------------------------------------------------


@pytest.fixture
def context(client, graph):
    return sharepoint.resolve_list(client, graph.SITE_ID, "Documents")


def test_query_filters_on_the_resolved_internal_name(client, graph, context):
    graph.add_item("1", status="PRINT_READY")
    graph.add_item("2", status="PRINT_COMPLETED")

    found = sharepoint.query_by_status(client, context, "PRINT_READY")

    assert [f.item_id for f in found] == ["1"]
    query = graph.calls_to("/items?", method="GET")[0].url
    assert "Print_x005f_Status+eq+%27PRINT_READY%27" in query.replace("%20", "+")


def test_query_sends_the_non_indexed_prefer_header(client, graph, context):
    """A non-indexed column cannot be used in $filter at all. The column should
    be indexed; this header makes a missing index degrade to "may fail under
    load" rather than "always fails"."""
    graph.add_item("1")
    sharepoint.query_by_status(client, context, "PRINT_READY")

    header = graph.calls_to("/items?", method="GET")[0].headers.get("Prefer", "")
    assert header == "HonorNonIndexedQueriesWarningMayFailRandomly"


def test_query_follows_pagination(client, graph, context):
    graph.page_size = 2
    for i in range(5):
        graph.add_item(str(i), created="2026-08-0{}T10:00:00Z".format(i + 1))

    found = sharepoint.query_by_status(client, context, "PRINT_READY")

    assert len(found) == 5
    assert len(graph.calls_to("/items", method="GET")) == 3  # 2 + 2 + 1


def test_query_scopes_to_the_folder(client, graph, context):
    graph.add_item("in", folder="/sites/Ops/Shared Documents/Invoices/ToPrint")
    graph.add_item("out", folder="/sites/Ops/Shared Documents/Payroll")

    found = sharepoint.query_by_status(client, context, "PRINT_READY",
                                       folder="/Invoices/ToPrint")

    assert [f.item_id for f in found] == ["in"]


def test_item_folder_falls_back_to_web_url_when_file_dir_ref_is_absent(
        graph, context):
    item = graph.add_item("1", name="invoice 1.pdf")
    item["fields"].pop("FileDirRef")
    item["webUrl"] = (
        "https://example.sharepoint.com/sites/PM/AI_DropBox_V2026/"
        "Backup/Invoice/invoice%201.pdf"
    )

    row = sharepoint._to_print_file(context, item)

    assert row.folder == (
        "/sites/PM/AI_DropBox_V2026/Backup/Invoice"
    )
    assert print_policy.folder_matches(row.folder, "/Backup/Invoice")


def test_query_maps_all_four_columns_onto_the_row(client, graph, context):
    graph.add_item("1", status="PRINT_PENDING", job_id="1825",
                   printer="share-guid", message="hello", name="bill.pdf")

    row = sharepoint.query_by_status(client, context, "PRINT_PENDING")[0]

    assert (row.status, row.job_id, row.printer, row.message) == (
        "PRINT_PENDING", "1825", "share-guid", "hello")
    assert row.file_name == "bill.pdf"
    assert row.etag == graph.items["1"]["eTag"]
    assert row.created is not None


# --- the claim ----------------------------------------------------------------


def test_patch_translates_display_names_to_internal_names(client, graph, context):
    graph.add_item("1")

    sharepoint.patch_fields(client, context, "1",
                            {print_policy.COLUMN_STATUS: "PRINT_PENDING"})

    body = graph.calls_to("/items/1/fields", method="PATCH")[0].body
    assert body == {"Print_x005f_Status": "PRINT_PENDING"}
    assert graph.status_of("1") == "PRINT_PENDING"


def test_patch_with_a_matching_etag_succeeds(client, graph, context):
    item = graph.add_item("1")
    assert sharepoint.patch_fields(
        client, context, "1", {print_policy.COLUMN_STATUS: "PRINT_PENDING"},
        etag=item["eTag"]) is True


def test_patch_with_a_stale_etag_returns_false(client, graph, context):
    """The whole double-print guard: of two overlapping runs, exactly one wins
    each file. A 412 is ordinary control flow, not an error."""
    item = graph.add_item("1")
    stale = item["eTag"]

    assert sharepoint.patch_fields(client, context, "1",
                                   {print_policy.COLUMN_STATUS: "PRINT_PENDING"},
                                   etag=stale) is True
    # The first write bumped the eTag; the second claim must lose.
    assert sharepoint.patch_fields(client, context, "1",
                                   {print_policy.COLUMN_STATUS: "PRINT_PENDING"},
                                   etag=stale) is False


def test_patch_without_an_etag_is_unconditional(client, graph, context):
    graph.add_item("1")
    graph.items["1"]["eTag"] = '"changed"'
    assert sharepoint.patch_fields(client, context, "1",
                                   {print_policy.COLUMN_MESSAGE: "x"}) is True


# --- downloading: no Authorization header ------------------------------------


def test_download_url_is_fetched_without_an_authorization_header(client, graph, context):
    """Graph: "You don't need to include an Authorization header when you access
    the download URL." Sending one is at best pointless and at worst rejected."""
    graph.add_item("1")
    info = sharepoint.get_download_url(client, context, "1")
    sharepoint.download(info["download_url"])

    download_call = graph.anon.calls[-1]
    assert download_call.method == "GET"
    assert "Authorization" not in download_call.headers


def test_get_download_url_returns_name_size_and_content_type(client, graph, context):
    graph.add_item("1", name="statement.pdf", size=4096)
    info = sharepoint.get_download_url(client, context, "1")
    assert info["name"] == "statement.pdf"
    assert info["size"] == 4096
    assert info["content_type"] == "application/pdf"


def test_the_drive_item_request_does_not_select_away_the_download_url(client, graph, context):
    """Defect L1, found in live testing on 2026-09-01 and reproduced by the fake.

    `@microsoft.graph.downloadUrl` is an annotation, not a property. A $select
    naming ordinary properties returns those properties and drops the
    annotations -- including this one, even when it is named in the same
    $select. The shipped code did exactly that, so EVERY real download failed
    with "no downloadable driveItem (is it a folder?)" on files that were
    perfectly ordinary PDFs.

    The offline suite could not see it: FakeGraph ignored $select and returned
    the URL regardless. It now models the service, which is what makes this test
    fail if the select comes back.
    """
    graph.add_item("1")
    info = sharepoint.get_download_url(client, context, "1")
    assert info["download_url"]

    request = graph.calls_to("/driveItem", method="GET")[-1]
    ordinary = [field for field in _select_fields(request.url)
                if not field.startswith("@")]
    assert not ordinary, (
        "the driveItem GET selects {} -- any ordinary property in $select "
        "drops the download URL annotation".format(", ".join(ordinary)))


def _select_fields(url: str) -> list:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return [field.strip() for field in (query.get("$select") or [""])[0].split(",")
            if field.strip()]


def test_a_reset_connection_is_retried_rather_than_failing_the_file(client, graph,
                                                                    context, no_sleep):
    """A GET of a document is idempotent, so a reset mid-handshake must not cost
    a claimed row.

    Measured on a workstation running TLS interception (2026-09-01): two of three
    identical attempts died with WinError 10054 and the third returned the file.
    Unretried, that is a PRINT_FAILED written for a reason outside the pipeline,
    and a human resetting the column by hand before anything can be re-run.
    """
    graph.add_item("1")
    graph.anon.download_resets = 2

    info = sharepoint.get_download_url(client, context, "1")
    assert sharepoint.download(info["download_url"]) == graph.anon.content

    assert len(graph.anon.calls_to("/download/1")) == 3
    assert len(no_sleep) == 2, "the retries did not back off"


def test_a_download_gives_up_after_the_attempt_limit(client, graph, context, no_sleep):
    """The retry is bounded. The download URL expires within minutes, so an
    endlessly retried GET would outlive the URL it is fetching."""
    graph.add_item("1")
    graph.anon.download_resets = 99

    info = sharepoint.get_download_url(client, context, "1")
    with pytest.raises(GraphError) as caught:
        sharepoint.download(info["download_url"])

    assert "10054" in str(caught.value), "the underlying cause must survive"
    assert len(graph.anon.calls_to("/download/1")) == graph_client.MAX_ATTEMPTS


def test_an_expired_download_url_is_not_retried(client, graph, context, no_sleep):
    """410 Gone means the URL expired, and every retry of it expires too. Only a
    transport failure and the documented retryable statuses are worth a second
    attempt."""
    graph.add_item("1")
    graph.anon.download_status = 410

    info = sharepoint.get_download_url(client, context, "1")
    with pytest.raises(GraphError):
        sharepoint.download(info["download_url"])

    assert len(graph.anon.calls_to("/download/1")) == 1
    assert no_sleep == []


def test_a_list_item_with_no_drive_item_is_an_error(client, graph, context):
    with pytest.raises(GraphError):
        sharepoint.get_download_url(client, context, "does-not-exist")


# --- printer preflight --------------------------------------------------------


def test_get_share_captures_capabilities_and_the_printer_id(client, graph):
    """The printer id is only needed for CANCEL, which is documented on the
    printers route rather than the shares route. Grabbing it during the preflight
    avoids an extra round trip at the moment it is needed."""
    share = universal_print.get_share(client, graph.SHARE_ID)

    assert share.share_id == graph.SHARE_ID
    assert share.printer_id == graph.PRINTER_ID
    assert share.accepting_jobs is True
    assert "application/pdf" in share.content_types


def test_share_supports_checks_the_content_type():
    share = universal_print.ShareInfo(
        share_id="s", printer_id="p", display_name="d", accepting_jobs=True,
        content_types=["application/pdf"])
    assert share.supports("application/pdf") is True
    assert share.supports("application/pdf; charset=binary") is True
    assert share.supports("application/vnd.openxmlformats-officedocument"
                          ".wordprocessingml.document") is False


def test_a_printer_reporting_no_content_types_gets_the_benefit_of_the_doubt():
    """Graph warns contentTypes is what the device reports and is not
    authoritative. Refusing to print because a printer under-reports would be
    worse than letting the upload fail with a real error."""
    share = universal_print.ShareInfo(share_id="s", printer_id="p",
                                      display_name="d", accepting_jobs=True,
                                      content_types=[])
    assert share.supports("application/pdf") is True


def test_missing_is_accepting_jobs_is_not_treated_as_refusal(client, graph):
    del graph.share["isAcceptingJobs"]
    assert universal_print.get_share(client, graph.SHARE_ID).accepting_jobs is True


def test_an_unknown_share_raises(client, graph):
    with pytest.raises(GraphError):
        universal_print.get_share(client, "no-such-share")


# --- submitting ---------------------------------------------------------------


def test_submit_document_runs_the_four_documented_calls_in_order(client, graph):
    job_id = universal_print.submit_document(
        client, graph.SHARE_ID, b"%PDF-1.7 hello", "a.pdf", "application/pdf")

    urls = [c.url for c in graph.calls if "/print/" in c.url]
    assert any(u.endswith("/jobs") for u in urls)
    assert any(u.endswith("/createUploadSession") for u in urls)
    assert any(u.endswith("/start") for u in urls)
    assert graph.jobs[job_id]["status"]["state"] == "processing"


def test_the_upload_put_carries_no_authorization_header(client, graph):
    """Graph: "Including the Authorization header when making the PUT call might
    result in an HTTP 401 Unauthorized." The upload URL carries its own token."""
    universal_print.submit_document(
        client, graph.SHARE_ID, b"%PDF-1.7 hello", "a.pdf", "application/pdf")

    puts = [c for c in graph.anon.calls if c.method == "PUT"]
    assert puts, "expected at least one upload PUT"
    for put in puts:
        assert "Authorization" not in put.headers


def test_a_small_document_uploads_in_one_range_ending_in_201(client, graph):
    data = b"x" * 1024
    universal_print.submit_document(client, graph.SHARE_ID, data, "a.pdf",
                                    "application/pdf")

    puts = [c for c in graph.anon.calls if c.method == "PUT"]
    assert len(puts) == 1
    assert puts[0].headers["Content-Range"] == "bytes 0-1023/1024"
    assert puts[0].headers["Content-Length"] == "1024"


def test_a_large_document_chunks_within_the_documented_limits(client, graph):
    """Graph caps a single PUT below 10 MB and recommends ranges that are a
    multiple of 200 KB. Every chunk but the last must satisfy both."""
    size = 9 * 1024 * 1024
    universal_print.submit_document(client, graph.SHARE_ID, b"z" * size,
                                    "big.pdf", "application/pdf")

    puts = [c for c in graph.anon.calls if c.method == "PUT"]
    assert len(puts) > 1

    for put in puts[:-1]:
        length = int(put.headers["Content-Length"])
        assert length < 10 * 1024 * 1024, "a chunk exceeded Graph's 10 MB limit"
        assert length % (200 * 1024) == 0, "a chunk was not a 200 KB multiple"

    # Contiguous, complete, and correctly terminated.
    uploaded = b"".join(bytes(v) for v in graph.anon.uploaded.values())
    assert len(uploaded) == size
    assert puts[-1].headers["Content-Range"].endswith("/{}".format(size))


def test_an_upload_that_never_returns_201_is_an_error(client, graph):
    """A silent 202 on the final chunk means the document is incomplete; starting
    the job would print nothing useful."""
    graph.anon.put_status_override = 202
    with pytest.raises(universal_print.PrintStageError) as excinfo:
        universal_print.submit_document(client, graph.SHARE_ID, b"data", "a.pdf",
                                        "application/pdf")
    assert excinfo.value.stage == universal_print.STAGE_UPLOAD


def test_a_failed_upload_deletes_the_session(client, graph):
    """An abandoned half-upload would otherwise hold a job that can never start
    until the session expires."""
    graph.anon.put_status_override = 500
    with pytest.raises(universal_print.PrintStageError):
        universal_print.submit_document(client, graph.SHARE_ID, b"data", "a.pdf",
                                        "application/pdf")
    assert graph.anon.deleted, "the upload session should have been cancelled"


def test_refuses_to_upload_an_empty_document(client, graph):
    with pytest.raises(universal_print.PrintStageError):
        universal_print.submit_document(client, graph.SHARE_ID, b"", "a.pdf",
                                        "application/pdf")


@pytest.mark.parametrize("fragment,stage", [
    ("/jobs", universal_print.STAGE_CREATE),
    ("createUploadSession", universal_print.STAGE_UPLOAD_SESSION),
])
def test_each_failure_is_tagged_with_its_stage(client, graph, fragment, stage):
    """The stage is what makes Print_Message actionable: "upload: 413" points at
    the document, "start_job: offline" points at the printer."""
    graph.fail_next("POST", fragment, status=500)
    with pytest.raises(universal_print.PrintStageError) as excinfo:
        universal_print.submit_document(client, graph.SHARE_ID, b"data", "a.pdf",
                                        "application/pdf")
    assert excinfo.value.stage == stage


# --- retry policy -------------------------------------------------------------


def test_a_429_is_retried_and_honours_retry_after(client, graph, no_sleep):
    graph._failures.append({
        "method": "GET", "match": "/print/shares/", "times": 1,
        "response": FakeResponse(429, {"error": {"code": "throttled",
                                                 "message": "slow down"}},
                                 headers={"Retry-After": "7"}),
    })
    share = universal_print.get_share(client, graph.SHARE_ID)

    assert share.share_id == graph.SHARE_ID  # the retry succeeded
    assert no_sleep == [7.0], "Retry-After was not honoured"


def test_a_403_is_not_retried(client, graph, no_sleep):
    """Permission problems do not improve with repetition; retrying just delays
    the real answer."""
    graph.fail_next("GET", "/print/shares/", status=403, times=5)
    with pytest.raises(GraphError) as excinfo:
        universal_print.get_share(client, graph.SHARE_ID)

    assert excinfo.value.status_code == 403
    assert no_sleep == []
    assert len(graph.calls_to("/print/shares/", method="GET")) == 1


def test_the_create_job_post_is_never_retried(client, graph, no_sleep):
    """NOT idempotent. A retry after a response we failed to read creates a
    second job and a second printed page."""
    graph.fail_next("POST", "/jobs", status=503, times=3)
    with pytest.raises(GraphError):
        universal_print.create_job(client, graph.SHARE_ID)

    posts = [c for c in graph.calls if c.method == "POST" and c.url.endswith("/jobs")]
    assert len(posts) == 1, "create_job must be attempted exactly once"
    assert no_sleep == []


def test_a_retryable_error_gives_up_after_the_attempt_limit(client, graph, no_sleep):
    graph.fail_next("GET", "/print/shares/", status=503, times=10)
    with pytest.raises(GraphError):
        universal_print.get_share(client, graph.SHARE_ID)
    assert len(graph.calls_to("/print/shares/", method="GET")) == graph_client.MAX_ATTEMPTS


def test_graph_errors_carry_the_correlation_ids(client, graph):
    """request-id and X-MSEdge-Ref are the only things Microsoft support can use;
    discarding them with the response object makes an outage undiagnosable."""
    graph._failures.append({
        "method": "GET", "match": "/print/shares/", "times": 1,
        "response": FakeResponse(500, {"error": {"code": "boom", "message": "bang"}},
                                 headers={"request-id": "req-123",
                                          "X-MSEdge-Ref": "edge-456"}),
    })
    with pytest.raises(GraphError) as excinfo:
        universal_print.get_share(client, graph.SHARE_ID)

    assert excinfo.value.request_id == "req-123"
    assert excinfo.value.edge_ref == "edge-456"
    assert "req-123" in str(excinfo.value)


# --- job status and cancellation ---------------------------------------------


def test_get_job_returns_none_for_a_purged_job(client, graph):
    """Finished jobs age out of Universal Print. A 404 is expected, not
    exceptional -- Poll writes nothing when it sees one."""
    assert universal_print.get_job(client, graph.SHARE_ID, "9999") is None


def test_job_state_and_description(client, graph):
    graph.add_job("1825", state="stopped", description="Out of paper",
                  details=["paperJam"])
    job = universal_print.get_job(client, graph.SHARE_ID, "1825")

    assert universal_print.job_state(job) == "stopped"
    assert "Out of paper" in universal_print.job_description(job)
    assert "paperJam" in universal_print.job_description(job)


def test_cancel_uses_the_printers_route_not_the_shares_route(client, graph):
    """Graph documents cancel only at /print/printers/{id}/jobs/{id}/cancel.
    Calling it on the share route 404s, which is why get_share captures the
    printer id."""
    graph.add_job("1825", state="stopped")

    assert universal_print.cancel_job(client, graph.PRINTER_ID, "1825") is True

    cancel_calls = graph.calls_to("/cancel", method="POST")
    assert len(cancel_calls) == 1
    assert "/print/printers/{}/jobs/1825/cancel".format(graph.PRINTER_ID) in cancel_calls[0].url
    assert graph.cancelled == ["1825"]


def test_cancelling_an_already_gone_job_counts_as_success(client, graph):
    """A 404 means it is already not going to print, which is the outcome we
    wanted."""
    assert universal_print.cancel_job(client, graph.PRINTER_ID, "no-such-job") is True


def test_a_failed_cancel_returns_false_rather_than_raising(client, graph):
    """Best-effort by contract: the caller is about to requeue the file, and
    refusing to do so because the cancel failed would leave it unprinted.
    False is the signal that a duplicate print is possible."""
    graph.add_job("1825", state="stopped")
    graph.fail_next("POST", "/cancel", status=500, times=3)

    assert universal_print.cancel_job(client, graph.PRINTER_ID, "1825") is False


def test_cancel_without_a_printer_id_is_a_no_op(client, graph):
    assert universal_print.cancel_job(client, "", "1825") is False


# --- content type -------------------------------------------------------------


@pytest.mark.parametrize("name,reported,expected", [
    ("a.pdf", "application/pdf", "application/pdf"),
    ("a.pdf", "", "application/pdf"),
    ("a.pdf", "application/pdf; charset=binary", "application/pdf"),
    ("noextension", "", "application/pdf"),
])
def test_guess_content_type(name, reported, expected):
    assert universal_print.guess_content_type(name, reported) == expected
