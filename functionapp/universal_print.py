"""
universal_print.py — Universal Print adapter.

Knows Graph's print URL shapes and the upload-session protocol. Knows NOTHING
about SharePoint or about what a Print_Status means.

Submitting a document is four calls, in this order, and none of them is optional:

    1  POST /print/shares/{share}/jobs                      -> jobId, documentId
    2  POST .../documents/{doc}/createUploadSession          -> uploadUrl
    3  PUT  {uploadUrl}   (one or more byte ranges)          -> 202 ... then 201
    4  POST .../jobs/{job}/start                             -> the job starts printing

A job created but never started sits at `paused`/`uploadPending` forever, which
is why step 4 exists and why a failure anywhere in 1-4 must be recorded as a
failure rather than left to look like a job in flight.

TWO THINGS THAT WILL BITE:

* The upload PUT must NOT carry an Authorization header. Graph: "Including the
  Authorization header when making the PUT call might result in an HTTP 401
  Unauthorized." The upload URL carries its own tempauthtoken. That is why
  uploads go through graph_client's unauthenticated session.

* CANCEL LIVES ON THE PRINTER ROUTE, NOT THE SHARE ROUTE. The documented path is
  POST /print/printers/{printerId}/jobs/{jobId}/cancel, so cancelling needs the
  PRINTER id even though everything else here uses the SHARE id. get_share()
  expands the share's `printer` relationship to capture it in the same call that
  does the preflight, so no extra round trip is needed later.

APP-ONLY DOES NOT WORK HERE. Creating, starting and cancelling a job are all
documented "Application: Not supported", and createUploadSession on a share is
"supported with delegated permissions only". See graph_auth.py.
"""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Optional, Tuple

import graph_client
import print_policy
from graph_client import GraphClient, GraphError

# Graph caps a single PUT below 10 MB and recommends byte ranges that are a
# multiple of 200 KB. 20 x 200 KB = 4,096,000 bytes: comfortably under the cap,
# an exact multiple, and large enough that ordinary documents go in one request.
CHUNK_MULTIPLE = 200 * 1024
CHUNK_SIZE = 20 * CHUNK_MULTIPLE

DEFAULT_CONTENT_TYPE = "application/pdf"


@dataclass
class ShareInfo:
    """The preflight result for a printer share.

    Everything past `state` exists so a printer profile can build a job
    configuration from what the device actually reports rather than from
    hardcoded values. They all come out of the same `capabilities` object
    get_share already fetches, so none of them costs a round trip.
    """
    share_id: str
    printer_id: str
    display_name: str
    accepting_jobs: bool
    content_types: List[str] = dataclass_field(default_factory=list)
    state: str = ""
    dpis: List[int] = dataclass_field(default_factory=list)
    scalings: List[str] = dataclass_field(default_factory=list)
    media_sizes: List[str] = dataclass_field(default_factory=list)
    colour_supported: bool = False
    top_margins: List[int] = dataclass_field(default_factory=list)
    bottom_margins: List[int] = dataclass_field(default_factory=list)
    left_margins: List[int] = dataclass_field(default_factory=list)
    right_margins: List[int] = dataclass_field(default_factory=list)

    def supports(self, content_type: str) -> bool:
        """Whether the printer accepts this MIME type.

        Graph warns that contentTypes is what the *printer* reports and "it is
        not guaranteed that the Universal Print service supports printing all of
        these MIME types" -- so a match here is necessary, not sufficient. A
        printer that reports nothing at all is given the benefit of the doubt:
        refusing to print because a device under-reports its capabilities would
        be worse than letting the upload fail with a real error.
        """
        if not self.content_types:
            return True
        wanted = (content_type or "").split(";")[0].strip().lower()
        return any(wanted == known.split(";")[0].strip().lower()
                   for known in self.content_types)


def guess_content_type(file_name: str, reported: str = "") -> str:
    """The content type to declare for the upload. Prefers what SharePoint says
    the file is, falls back to the extension, then to PDF."""
    if reported:
        return reported.split(";")[0].strip()
    guessed, _ = mimetypes.guess_type(file_name or "")
    return guessed or DEFAULT_CONTENT_TYPE


# --- preflight ----------------------------------------------------------------


def get_share(client: GraphClient, share_id: str) -> ShareInfo:
    """Read a printer share, its capabilities, and its printer id in one call.

    Called once per invocation, before any file is claimed, so an offline printer
    or an unsupported document type is discovered while the queue is still
    untouched -- rather than after five files have been marked PRINT_PENDING.
    """
    url = (f"/print/shares/{share_id}"
           f"?$select=id,displayName,isAcceptingJobs,capabilities,status"
           f"&$expand=printer($select=id)")
    payload = client.get_json(url) or {}

    capabilities = payload.get("capabilities") or {}
    status = payload.get("status") or {}
    printer = payload.get("printer") or {}

    def ints(name: str) -> List[int]:
        return [int(v) for v in (capabilities.get(name) or [])
                if isinstance(v, (int, float)) or str(v).lstrip("-").isdigit()]

    return ShareInfo(
        share_id=payload.get("id") or share_id,
        printer_id=printer.get("id") or "",
        display_name=payload.get("displayName") or "",
        # Absent means unknown, not "refusing"; only an explicit False blocks.
        accepting_jobs=payload.get("isAcceptingJobs") is not False,
        content_types=list(capabilities.get("contentTypes") or []),
        state=(status.get("state") or ""),
        dpis=ints("dpis"),
        scalings=[str(v) for v in (capabilities.get("scalings") or [])],
        media_sizes=[str(v) for v in (capabilities.get("mediaSizes") or [])],
        colour_supported=bool(capabilities.get("isColorPrintingSupported")),
        top_margins=ints("topMargins"),
        bottom_margins=ints("bottomMargins"),
        left_margins=ints("leftMargins"),
        right_margins=ints("rightMargins"),
    )


# --- submitting ---------------------------------------------------------------


def create_job(client: GraphClient, share_id: str,
               configuration: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """Create a print job. Returns (job_id, document_id).

    retry=False is deliberate and important: this call is NOT idempotent. A retry
    after a response we failed to read would create a second job and print the
    document twice. A transient failure here costs one retry cycle; a duplicate
    costs paper and trust.

    `configuration` lets a printer profile send the settings its own output needs
    -- a pre-rasterized document, for instance, must carry the scaling and
    margins that place it on the page. Omitted, it falls back to
    print_policy.JOB_CONFIGURATION, which stays deliberately minimal so an
    ordinary printer's own defaults decide colour, duplex and quality.
    """
    payload = client.post_json(
        f"/print/shares/{share_id}/jobs",
        {"configuration": dict(configuration if configuration is not None
                               else print_policy.JOB_CONFIGURATION)},
        expected=(200, 201), retry=False,
    ) or {}

    job_id = str(payload.get("id") or "")
    documents = payload.get("documents") or []
    document_id = str(documents[0].get("id")) if documents else ""
    if not job_id or not document_id:
        raise GraphError(f"print job creation returned no job/document id: {payload}")
    return job_id, document_id


def create_upload_session(client: GraphClient, share_id: str, job_id: str,
                          document_id: str, file_name: str,
                          content_type: str, size: int) -> str:
    payload = client.post_json(
        f"/print/shares/{share_id}/jobs/{job_id}/documents/{document_id}/createUploadSession",
        {"properties": {"documentName": file_name,
                        "contentType": content_type,
                        "size": size}},
        expected=(200, 201),
    ) or {}
    upload_url = payload.get("uploadUrl")
    if not upload_url:
        raise GraphError(f"createUploadSession returned no uploadUrl: {payload}")
    return upload_url


def upload_document(upload_url: str, data: bytes,
                    timeout: Optional[float] = None) -> None:
    """Upload the bytes in sequential ranges.

    Graph answers 202 with nextExpectedRanges while more is expected and 201 when
    the last range lands, so the 201 is the completion signal and is asserted --
    a silent 202 on the final chunk would mean the document is incomplete and the
    job would start and print nothing useful.

    On any failure the session is deleted, so an abandoned half-upload does not
    sit until its expiry holding a job that can never start.
    """
    total = len(data)
    if total == 0:
        raise GraphError("refusing to upload an empty document")

    try:
        final_status = None
        for start in range(0, total, CHUNK_SIZE):
            end = min(start + CHUNK_SIZE, total) - 1
            chunk = data[start:end + 1]
            response = graph_client.put_unauthenticated(
                upload_url, chunk,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Length": str(len(chunk)),
                },
                timeout=timeout,
            )
            final_status = response.status_code

        if final_status not in (200, 201):
            raise GraphError(
                f"upload finished with HTTP {final_status}, expected 201; "
                f"the document is incomplete"
            )
    except Exception:
        graph_client.delete_unauthenticated(upload_url, timeout=timeout)
        raise


def start_job(client: GraphClient, share_id: str, job_id: str) -> Dict[str, Any]:
    """Release the job to the printer. Returns the printJobStatus."""
    return client.post_json(f"/print/shares/{share_id}/jobs/{job_id}/start",
                            expected=(200, 202)) or {}


STAGE_CREATE = "create_job"
STAGE_UPLOAD_SESSION = "upload_session"
STAGE_UPLOAD = "upload"
STAGE_START = "start_job"


class PrintStageError(RuntimeError):
    """A submission failure, tagged with the step that failed.

    The stage is what makes Print_Message actionable. "upload: 413 payload too
    large" sends someone to the document; "start_job: printer offline" sends them
    to the printer. Without it every failure reads the same and the column is
    just noise.
    """

    def __init__(self, stage: str, error: Any):
        super().__init__(str(error))
        self.stage = stage
        self.original = error


def submit_document(client: GraphClient, share_id: str, data: bytes,
                    file_name: str, content_type: str,
                    timeout: Optional[float] = None,
                    configuration: Optional[Dict[str, Any]] = None) -> str:
    """The whole four-step submission. Returns the job id.

    Every failure is re-raised as a PrintStageError naming the step, so the
    caller can write a Print_Message that points at the right system.

    `configuration` is passed straight to create_job; see there for why a
    printer profile may need to override the default.
    """
    try:
        job_id, document_id = create_job(client, share_id, configuration)
    except Exception as exc:
        raise PrintStageError(STAGE_CREATE, exc)

    try:
        upload_url = create_upload_session(client, share_id, job_id, document_id,
                                           file_name, content_type, len(data))
    except Exception as exc:
        raise PrintStageError(STAGE_UPLOAD_SESSION, exc)

    try:
        upload_document(upload_url, data, timeout=timeout)
    except Exception as exc:
        raise PrintStageError(STAGE_UPLOAD, exc)

    try:
        start_job(client, share_id, job_id)
    except Exception as exc:
        raise PrintStageError(STAGE_START, exc)

    return job_id


# --- status and cancellation --------------------------------------------------


def get_job(client: GraphClient, share_id: str, job_id: str) -> Optional[Dict[str, Any]]:
    """A print job, or None if Universal Print no longer has it (404).

    A 404 is expected, not exceptional: finished jobs age out of the service.
    Poll writes nothing when it sees one -- guessing "it must have printed" would
    mark documents complete that may never have printed at all.
    """
    return client.get_json(f"/print/shares/{share_id}/jobs/{job_id}",
                           allow_status=(404,))


def job_state(job: Optional[Dict[str, Any]]) -> str:
    if not job:
        return ""
    return ((job.get("status") or {}).get("state") or "").strip().lower()


def job_acknowledged_at(job: Optional[Dict[str, Any]]) -> Optional[str]:
    """printJob.acknowledgedDateTime, or None.

    Documented as "the dateTimeOffset when the job was acknowledged" -- the
    closest thing printJob has to a print time, and much closer to the paper than
    the moment Poll happened to run. printJob has no completion field; see
    print_policy.completion_time for why this is used anyway.

    IT NOW HAS A SECOND READER. Since R24 it is also the preferred STALL clock --
    the threshold asks how long the printer has held the job, not how long ago we
    created it. `print_policy.stall_clock_start` decides when it is usable and
    falls back to createdDateTime; the two questions are separate and their
    helpers must stay separate.
    """
    return (job or {}).get("acknowledgedDateTime") or None


def job_created_at(job: Optional[Dict[str, Any]]) -> Optional[str]:
    """printJob.createdDateTime, or None.

    THE ATTEMPT CLOCK, AND THE FALLBACK STALL CLOCK. Poll reads how many retries
    are already spent from this, and measures the stall threshold from it whenever
    the job carries no usable acknowledgement (R24 moved the preference to
    acknowledgedDateTime; see print_policy.stall_clock_start). Never from anything
    in SharePoint, because this belongs to the job: nobody editing the library row
    can reset it, and it survives a requeue by virtue of the replacement being a
    different job with its own value.

    Costs no extra call -- `get_job` fetches the whole resource with no $select,
    so the field is already in the response. Confirmed present on a live job in
    tests/test_acknowledged_time.py.
    """
    return (job or {}).get("createdDateTime") or None


def job_description(job: Optional[Dict[str, Any]]) -> str:
    """A human-readable reason, for Print_Message on a failed job."""
    status = (job or {}).get("status") or {}
    description = (status.get("description") or "").strip()
    details = status.get("details") or []
    if details:
        return f"{description} ({', '.join(str(d) for d in details)})".strip()
    return description or (status.get("state") or "unknown state")


def cancel_job(client: GraphClient, printer_id: str, job_id: str) -> bool:
    """Cancel a job so a replacement cannot print alongside it. True if cancelled.

    Best-effort by contract. The caller is about to requeue or abandon the file;
    refusing to do so because the cancel failed would leave a document unprinted,
    which is the worse outcome. But a False return is logged loudly, because it is
    the only signal that a duplicate print is possible.

    Note the PRINTER route -- cancel is not documented on /print/shares/...
    """
    if not printer_id or not job_id:
        logging.warning("cannot cancel job %r: no printer id available", job_id)
        return False
    try:
        client.post_json(f"/print/printers/{printer_id}/jobs/{job_id}/cancel",
                         expected=(200, 202, 204), retry=False)
        return True
    except GraphError as exc:
        # 404 means it is already gone, which is exactly what we wanted.
        if exc.status_code == 404:
            return True
        logging.warning("could not cancel print job %s on printer %s: %s. "
                        "Continuing anyway -- a duplicate print is possible.",
                        job_id, printer_id, exc)
        return False
