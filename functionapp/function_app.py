"""
function_app.py — the three HTTP endpoints Power Automate calls.

    Submit    POST /api/print/submit     claim PRINT_READY files, create print jobs
    Poll      POST /api/print/status     check PRINT_PENDING jobs, mark the finished
    Resubmit  POST /api/print/resubmit   cancel + retry outstanding jobs over 72h old

This module is a THIN ORCHESTRATOR. It sequences calls and shapes responses. It
contains no rules: every threshold, status string, window and message format
lives in print_policy, and every HTTP shape lives in the two adapters. If you
find yourself writing `if status == ...` here, it belongs in print_policy
(docs/design.md §5.2, §9).

THE ORDERING THAT MATTERS. Each file is CLAIMED -- an eTag-conditioned PATCH
setting PRINT_PENDING -- BEFORE its print job is created. The requirement writes
the status afterwards; this is a deliberate deviation, recorded at R5, because
the two orderings fail differently:

    claim first   a crash loses the print; Resubmit recovers it after 72h
    claim last    a crash prints the document twice; nothing recovers that

A recoverable lost print beats a silent double print. The consequence, which
Poll must respect: a PRINT_PENDING row with an empty Print_JobId is normal, not
an error -- it is a crashed submission waiting for Resubmit.

Two log lines carry all the reporting (docs/design.md §13):
    RUN_SUMMARY   one per invocation
    PRINT_EVENT   one per file per outcome
Fields are embedded in the message text because the Python worker does not map
logging `extra=` into customDimensions, and free text goes LAST so KQL `parse`
delimiters stay unambiguous. Sampling must stay disabled in host.json or these
rows are dropped and every count in the workbook is silently wrong.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import azure.functions as func

import graph_auth
import graph_client
import print_policy
import sharepoint
import universal_print
from graph_client import GraphClient
from sharepoint import ColumnNotFound, ListContext, PrintFile

app = func.FunctionApp()

EP_SUBMIT = "submit"
EP_POLL = "poll"
EP_RESUBMIT = "resubmit"


# --- responses and logging ----------------------------------------------------


def _json_response(status: int, payload: dict) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False, default=str),
        status_code=status, mimetype="application/json",
    )


def _run_summary(endpoint: str, *, library: str = "-", folder: str = "-",
                 printer: str = "-", found: int = -1, ok: int = -1,
                 failed: int = -1, skipped: int = -1, remaining: int = -1,
                 http_status: int = 0, started: Optional[float] = None) -> None:
    """One stable, greppable line per invocation. Every dashboard and alert keys
    on it. Sentinels (-1, "-") mean "never reached" -- a missing key would break
    the KQL parse, while a sentinel is a countable fact."""
    elapsed = int((time.monotonic() - started) * 1000) if started else -1
    logging.info(
        "RUN_SUMMARY ep=%s lib=%s printer=%s found=%s ok=%s failed=%s skipped=%s "
        "remaining=%s httpStatus=%s ms=%s folder=%s",
        endpoint, library or "-", printer or "-", found, ok, failed, skipped,
        remaining, http_status, elapsed, folder or "-",
    )


def _print_event(endpoint: str, item: str, *, from_status: str = "-",
                 to_status: str = "-", job: str = "-", printer: str = "-",
                 result: str = "-", ms: int = -1, file_name: str = "-") -> None:
    """One line per file per outcome. This is the ONLY source that can answer
    'how many were submitted / retried / completed last week' -- the SharePoint
    columns hold just the latest state, so a file retried three times looks
    identical to one retried once."""
    logging.info(
        "PRINT_EVENT ep=%s item=%s from=%s to=%s job=%s printer=%s result=%s ms=%s file=%s",
        endpoint, item or "-", from_status or "-", to_status or "-", job or "-",
        printer or "-", result, ms, file_name or "-",
    )


# --- request plumbing ---------------------------------------------------------


class BadRequest(ValueError):
    """A caller error. Always answered with 400 and, critically, BEFORE anything
    is claimed -- a request that 400s must never touch the queue, or its own
    corrected retry would find the files already taken."""


def _body(req: func.HttpRequest) -> dict:
    try:
        payload = req.get_json()
    except ValueError:
        raise BadRequest("request body must be JSON")
    if not isinstance(payload, dict):
        raise BadRequest("request body must be a JSON object")
    return payload


def _required_str(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"{key} is required")
    return value.strip()


def _client() -> GraphClient:
    return GraphClient(graph_auth.get_access_token)


def _required_folder(body: dict) -> str:
    """`folder` is part of the requirement's input contract even when it
    addresses the library root, so an absent key is a 400 while an empty string
    is a legitimate "the whole library"."""
    if "folder" not in body:
        raise BadRequest('folder is required (use "" or "/" for the library root)')
    return print_policy.normalize_folder(body.get("folder"))


def _tunable(resolver, value):
    """Resolve a tunable, turning an out-of-range REQUEST value into a 400.

    print_policy raises ValueError for a value the caller asked for and cannot
    have. Without this translation it would fall through to the generic handler
    and surface as a 500 -- telling the caller the server is broken when in fact
    their request was.
    """
    try:
        return resolver(value)
    except ValueError as exc:
        raise BadRequest(str(exc))


def _resolve_list(client: GraphClient, library: str) -> ListContext:
    """Resolve the site and library, and with them the four column internal names."""
    import os
    hostname = os.getenv("SHAREPOINT_HOSTNAME")
    site_path = os.getenv("SHAREPOINT_SITE_PATH")
    if not hostname:
        raise RuntimeError("SHAREPOINT_HOSTNAME is not set; it names this "
                           "environment and has no safe default")

    site_id = sharepoint.resolve_site(client, hostname, site_path or "")
    return sharepoint.resolve_list(client, site_id, library)


def _share_for(client: GraphClient, cache: Dict[str, Any], share_id: str):
    """A printer share, cached per invocation, or None if it cannot be resolved.

    Returns None rather than raising because one of its callers is the
    best-effort cancel: a share we can no longer look up must not abort a
    resubmission.
    """
    if not share_id:
        return None
    if share_id not in cache:
        try:
            cache[share_id] = universal_print.get_share(client, share_id)
        except Exception:
            logging.warning("could not resolve printer share %r", share_id,
                            exc_info=True)
            cache[share_id] = None
    return cache[share_id]


class Budget:
    """Wall-clock guard. Checked BEFORE starting each file, never mid-file, so
    the cap can never strand a claimed-but-unsubmitted row.

    `started` anchors it at the moment the REQUEST arrived, not at the moment the
    file loop begins. Site resolution, the printer preflight and the SharePoint
    query all happen first and can be slow; timing only the loop meant that work
    was free, so a run could still sail past Power Automate's ~120 s connector
    budget -- and a connector that has given up never receives the response, so
    the flow neither loops nor notices.
    """

    def __init__(self, seconds: Optional[float] = None,
                 started: Optional[float] = None):
        self._limit = seconds if seconds is not None else print_policy.resolve_budget_seconds()
        self._started = started if started is not None else time.monotonic()

    @property
    def exhausted(self) -> bool:
        return (time.monotonic() - self._started) >= self._limit


# --- the shared per-file submission -------------------------------------------


RESULT_SUBMITTED = "submitted"
RESULT_RESUBMITTED = "resubmitted"
RESULT_FAILED = "failed"
RESULT_SKIPPED = "skipped"


def _submit_one(client: GraphClient, context: ListContext, share: universal_print.ShareInfo,
                item: PrintFile, endpoint: str,
                result_name: str = RESULT_SUBMITTED) -> Dict[str, Any]:
    """Claim one file and put it on the printer.

    Used verbatim by BOTH Submit and Resubmit -- one code path, so a fix or a
    regression can only happen in one place, and one set of tests covers both.

    Returns a per-item record for the response. Never raises for an ordinary
    failure: a document that cannot print is recorded on the file itself and the
    batch continues.
    """
    started = time.monotonic()
    # The SHARE id, deliberately named as such. Printer_Name holds the share
    # id (design G2), and cancel is the ONE call that needs the printer id
    # instead -- calling the two by the same name here is how F3 happened.
    share_id = share.share_id

    # --- claim ---------------------------------------------------------------
    # eTag-conditioned: a 412 means a concurrent invocation got there first.
    # This is the entire double-print guard for overlapping scheduled runs.
    claimed = sharepoint.patch_fields(client, context, item.item_id, {
        print_policy.COLUMN_STATUS: print_policy.PENDING,
        print_policy.COLUMN_PRINTER: share_id,
        print_policy.COLUMN_JOB_ID: "",
        print_policy.COLUMN_MESSAGE: "",
    }, etag=item.etag)

    if not claimed:
        _print_event(endpoint, item.item_id, from_status=item.status,
                     result=RESULT_SKIPPED, printer=share_id,
                     file_name=item.file_name,
                     ms=int((time.monotonic() - started) * 1000))
        return {"itemId": item.item_id, "fileName": item.file_name,
                "result": RESULT_SKIPPED,
                "message": "another run claimed this file first"}

    # --- fetch, submit -------------------------------------------------------
    stage = "download"
    try:
        drive_item = sharepoint.get_download_url(client, context, item.item_id)
        file_name = drive_item["name"] or item.file_name
        content_type = universal_print.guess_content_type(
            file_name, drive_item.get("content_type", ""))

        if not share.supports(content_type):
            raise universal_print.PrintStageError(
                "content_type",
                f"printer {share.display_name or share.share_id} does not accept "
                f"{content_type} (supports: {', '.join(share.content_types) or 'unknown'})")

        data = sharepoint.download(drive_item["download_url"])

        job_id = universal_print.submit_document(
            client, share.share_id, data, file_name, content_type)

    except Exception as exc:
        stage = getattr(exc, "stage", stage)
        message = print_policy.failure_message(stage, exc)
        logging.warning("submission failed for item %s at stage %s: %s",
                        item.item_id, stage, exc, exc_info=True)
        # Record the failure on the file. The claim already wrote the printer, so
        # only the status and the message change.
        try:
            sharepoint.patch_fields(client, context, item.item_id, {
                print_policy.COLUMN_STATUS: print_policy.FAILED,
                print_policy.COLUMN_PRINTER: share_id,
                print_policy.COLUMN_MESSAGE: message,
            })
        except Exception:
            # The file is left at PRINT_PENDING with no job id, which Resubmit
            # recovers after 72h. Losing the message is bad; losing the file is worse.
            logging.exception("could not record the failure on item %s", item.item_id)

        _print_event(endpoint, item.item_id, from_status=item.status,
                     to_status=print_policy.FAILED, printer=share_id,
                     result=RESULT_FAILED, file_name=item.file_name,
                     ms=int((time.monotonic() - started) * 1000))
        return {"itemId": item.item_id, "fileName": item.file_name,
                "result": RESULT_FAILED, "message": message}

    # --- record the job id ---------------------------------------------------
    # Status is already PRINT_PENDING from the claim; only the job id is new.
    #
    # THIS WRITE IS GUARDED, AND THE REASON IS NOT TIDINESS. By the time we get
    # here the job has been created AND started: paper is on its way. If this
    # PATCH raises and we let it out, two things happen, both bad. The batch dies
    # with a 500, so files that printed perfectly well are never reported to the
    # flow. And the row is left at PRINT_PENDING with an empty Print_JobId --
    # which this design defines as "a crashed submission Resubmit owns" (§5.3),
    # so in 72 hours Resubmit prints the document a second time. That is exactly
    # the silent double print the claim-first ordering exists to prevent,
    # reintroduced one line from the end.
    #
    # We cannot recover the write, so we do the two things we can: keep going,
    # and say so loudly. The log line is the ONLY warning that a reprint is
    # coming, and PRINT_EVENT still carries the job id, so the correlation
    # survives even though the column does not.
    warning = ""
    try:
        sharepoint.patch_fields(client, context, item.item_id,
                                {print_policy.COLUMN_JOB_ID: job_id})
    except Exception:
        warning = (f"possible duplicate print: job {job_id} was started but its id "
                   f"could not be written to SharePoint, so Resubmit will treat "
                   f"this row as a crashed submission and may print it a second "
                   f"time after {print_policy.DEFAULT_MIN_AGE_HOURS}h")
        logging.error("%s (item %s, printer %s)",
                      warning, item.item_id, share_id, exc_info=True)

    _print_event(endpoint, item.item_id, from_status=item.status,
                 to_status=print_policy.PENDING, job=job_id, printer=share_id,
                 result=result_name, file_name=item.file_name,
                 ms=int((time.monotonic() - started) * 1000))
    record = {"itemId": item.item_id, "fileName": item.file_name,
              "result": result_name, "jobId": job_id}
    if warning:
        record["warning"] = warning
    return record


# --- Submit -------------------------------------------------------------------


@app.route(route="print/submit", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def submit_print_jobs(req: func.HttpRequest) -> func.HttpResponse:
    started = time.monotonic()
    library = folder = printer_share_id = "-"
    try:
        # Everything the caller can get wrong is checked first, in the order the
        # requirement lists the inputs, and before a single Graph call.
        body = _body(req)
        library = _required_str(body, "library")
        folder = _required_folder(body)
        printer_share_id = _required_str(body, "printerShareId")
        batch_size = _tunable(print_policy.resolve_batch_size, body.get("batchSize"))

        client = _client()
        context = _resolve_list(client, library)

        # Preflight BEFORE anything is claimed: an offline printer or an
        # unsupported document type must be discovered while the queue is
        # untouched, not after five files have been marked PRINT_PENDING.
        share = universal_print.get_share(client, printer_share_id)
        if not share.accepting_jobs:
            _run_summary(EP_SUBMIT, library=library, folder=folder,
                         printer=printer_share_id, found=0, ok=0, failed=0,
                         skipped=0, remaining=-1, http_status=200, started=started)
            return _json_response(200, {
                "library": library, "folder": folder, "printerShareId": printer_share_id,
                # remainingReady must be 0, NOT a "we didn't look" sentinel. Flow A
                # runs `Do Until remainingReady = 0`, and a negative number can
                # never satisfy it -- so an offline printer spun the loop to its
                # iteration cap on every recurrence. Nothing is submittable this
                # cycle, which is what 0 means to the flow: stop looping.
                "candidatesFound": 0, "remainingReady": 0, "submitted": 0,
                "failed": 0, "skipped": 0, "budgetExhausted": False,
                # ...and the flow needs SOMETHING to notify on. This run is a 200
                # with failed = 0, which matches no error condition, so without an
                # explicit flag an offline printer is completely silent.
                "printerAvailable": False, "items": [],
                "message": f"printer share {share.display_name or printer_share_id} "
                           f"is not accepting jobs (state: {share.state or 'unknown'})",
            })

        candidates = sharepoint.query_by_status(
            client, context, print_policy.READY, folder)
        selected = print_policy.select_oldest(candidates, batch_size)

        # Dry run: prove the wiring without printing anything. Deliberately a
        # mode of the endpoint rather than a separate script, so it exercises the
        # SAME auth, resolution and query the real run uses -- a harness with its
        # own code path proves nothing about the deployed app.
        if body.get("dryRun"):
            _run_summary(EP_SUBMIT, library=library, folder=folder,
                         printer=printer_share_id, found=len(candidates), ok=0,
                         failed=0, skipped=0, remaining=len(candidates),
                         http_status=200, started=started)
            return _json_response(200, {
                "dryRun": True,
                "library": context.list_title, "folder": folder,
                "resolvedColumns": {display: context.internal(display)
                                    for display in print_policy.COLUMN_DISPLAY_NAMES},
                "printer": {
                    "shareId": share.share_id,
                    "printerId": share.printer_id,
                    "displayName": share.display_name,
                    "acceptingJobs": share.accepting_jobs,
                    "state": share.state,
                    "contentTypes": share.content_types,
                },
                "candidatesFound": len(candidates),
                "wouldSubmit": [{"itemId": f.item_id, "fileName": f.file_name,
                                 "created": f.created} for f in selected],
            })

        budget = Budget(started=started)
        items: List[Dict[str, Any]] = []
        for item in selected:
            if budget.exhausted:
                logging.info("stopping after %s of %s files: wall-clock budget spent",
                             len(items), len(selected))
                break
            items.append(_submit_one(client, context, share, item, EP_SUBMIT))

        submitted = sum(1 for i in items if i["result"] == RESULT_SUBMITTED)
        failed = sum(1 for i in items if i["result"] == RESULT_FAILED)
        skipped = sum(1 for i in items if i["result"] == RESULT_SKIPPED)
        # Everything still PRINT_READY: the candidates we did not take, plus the
        # ones we skipped because another run claimed them. The flow loops while
        # this is > 0, so it must never overcount or the Do-Until never ends.
        remaining = max(0, len(candidates) - submitted - failed)

        _run_summary(EP_SUBMIT, library=library, folder=folder, printer=printer_share_id,
                     found=len(candidates), ok=submitted, failed=failed,
                     skipped=skipped, remaining=remaining, http_status=200,
                     started=started)
        return _json_response(200, {
            "library": library, "folder": folder, "printerShareId": printer_share_id,
            "batchSize": batch_size, "candidatesFound": len(candidates),
            "remainingReady": remaining, "submitted": submitted, "failed": failed,
            "skipped": skipped, "budgetExhausted": budget.exhausted,
            # Present on EVERY response, not just the failing one, so the flow can
            # test it without a null check.
            "printerAvailable": True, "items": items,
        })

    except BadRequest as exc:
        _run_summary(EP_SUBMIT, library=library, folder=folder,
                     printer=printer_share_id, http_status=400, started=started)
        return _json_response(400, {"error": str(exc)})
    except Exception as exc:
        return _server_error(EP_SUBMIT, exc, library, folder, printer_share_id, started)


# --- Poll ---------------------------------------------------------------------


@app.route(route="print/status", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def poll_print_status(req: func.HttpRequest) -> func.HttpResponse:
    started = time.monotonic()
    library = folder = "-"
    try:
        body = _body(req)
        library = _required_str(body, "library")
        folder = _required_folder(body)
        window_days = _tunable(print_policy.resolve_window_days, body.get("windowDays"))

        client = _client()
        context = _resolve_list(client, library)

        pending = sharepoint.query_by_status(
            client, context, print_policy.PENDING, folder)

        now = print_policy.now_utc()
        in_window = [f for f in pending
                     if print_policy.within_window(f.created, window_days, now)]

        # Files past the window are excluded by BOTH Poll and Resubmit, so
        # nothing will ever touch them again (docs/design.md G1). Reporting them
        # costs no extra call -- they are already in the result set -- and it is
        # the only automatic signal that a document is stranded.
        stale = [f for f in pending
                 if not print_policy.within_window(f.created, window_days, now)]

        # EVERY job in the window is checked. The requirement says "query the
        # sharepoint folder for ALL files ... with PRINT_STATUS = PRINT_PENDING",
        # and unlike Submit there is no loop in the Poll flow to collect a
        # remainder. Capping it also starved: taking the oldest N meant a handful
        # of long-running jobs occupied every slot run after run, so a newer job
        # that HAD completed was never marked and eventually aged past the 20-day
        # window into limbo. Only the wall-clock budget bounds this now, and what
        # it leaves behind is reported rather than silently dropped.
        ordered = print_policy.select_oldest(in_window, len(in_window))

        budget = Budget(started=started)
        items: List[Dict[str, Any]] = []
        completed = failed = still_running = not_found = malformed = 0
        visited = awaiting_resubmit = 0

        for item in ordered:
            if budget.exhausted:
                logging.info("stopping after %s of %s pending jobs: budget spent",
                             visited, len(ordered))
                break
            visited += 1

            # A PRINT_PENDING row with no job id is a crashed submission, not an
            # error: Submit claimed it and died before creating the job. Resubmit
            # owns it. Polling it would be meaningless -- but it is still COUNTED.
            # This is the one state the design tells you to expect (§5.3), and it
            # used to appear in no counter at all, so the numbers did not add up
            # to the rows in the window and a growing pile of crashed submissions
            # was invisible until Resubmit happened to pick them up 72h later.
            if not item.job_id:
                awaiting_resubmit += 1
                continue
            if not item.printer:
                malformed += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "malformed",
                              "message": "Print_JobId is set but Printer_Name is empty"})
                continue

            job = universal_print.get_job(client, item.printer, item.job_id)
            if job is None:
                not_found += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "not_found", "jobId": item.job_id,
                              "message": "the job is no longer known to Universal Print"})
                _print_event(EP_POLL, item.item_id, from_status=item.status,
                             job=item.job_id, printer=item.printer,
                             result="not_found", file_name=item.file_name)
                continue

            state = universal_print.job_state(job)
            action = print_policy.poll_action(state)

            if action == print_policy.POLL_COMPLETE:
                # The PRINTER's acknowledgement if it gave one, else the moment
                # this job was observed complete. Never the moment the request
                # began: `now` is deliberately still used for the window maths,
                # where one consistent instant matters, but using it here would
                # stamp forty jobs with a single time up to a minute and a half
                # stale. See print_policy.completion_time for why the printer's
                # own timestamp beats either of ours.
                message = print_policy.printed_on_message(
                    print_policy.completion_time(
                        universal_print.job_acknowledged_at(job),
                        print_policy.now_utc()))
                sharepoint.patch_fields(client, context, item.item_id, {
                    print_policy.COLUMN_STATUS: print_policy.COMPLETED,
                    print_policy.COLUMN_MESSAGE: message,
                })
                completed += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "completed", "jobId": item.job_id,
                              "message": message})
                _print_event(EP_POLL, item.item_id, from_status=item.status,
                             to_status=print_policy.COMPLETED, job=item.job_id,
                             printer=item.printer, result="completed",
                             file_name=item.file_name)

            elif action == print_policy.POLL_FAIL:
                message = print_policy.truncate_message(
                    universal_print.job_description(job))
                sharepoint.patch_fields(client, context, item.item_id, {
                    print_policy.COLUMN_STATUS: print_policy.FAILED,
                    print_policy.COLUMN_MESSAGE: message,
                })
                failed += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "failed_terminal", "jobId": item.job_id,
                              "message": message})
                _print_event(EP_POLL, item.item_id, from_status=item.status,
                             to_status=print_policy.FAILED, job=item.job_id,
                             printer=item.printer, result="failed_terminal",
                             file_name=item.file_name)

            else:
                still_running += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "still_running", "jobId": item.job_id,
                              "message": state})

        _run_summary(EP_POLL, library=library, folder=folder, found=len(in_window),
                     ok=completed, failed=failed, skipped=still_running,
                     remaining=-1, http_status=200, started=started)
        return _json_response(200, {
            "library": library, "folder": folder, "windowDays": window_days,
            "checked": len(items), "completed": completed, "failed": failed,
            "stillRunning": still_running, "notFound": not_found,
            "malformed": malformed, "budgetExhausted": budget.exhausted,
            # These four account for every row in the window, exactly once:
            #   checked + awaitingResubmit + uncheckedCount == pendingInWindow
            "pendingInWindow": len(ordered),
            "awaitingResubmit": awaiting_resubmit,
            "uncheckedCount": len(ordered) - visited,
            "staleCount": len(stale),
            "staleItems": [{"itemId": f.item_id, "fileName": f.file_name,
                            "created": f.created, "jobId": f.job_id}
                           for f in stale],
            "items": items,
        })

    except BadRequest as exc:
        _run_summary(EP_POLL, library=library, folder=folder,
                     http_status=400, started=started)
        return _json_response(400, {"error": str(exc)})
    except Exception as exc:
        return _server_error(EP_POLL, exc, library, folder, "-", started)


# --- Resubmit -----------------------------------------------------------------


@app.route(route="print/resubmit", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def resubmit_print_jobs(req: func.HttpRequest) -> func.HttpResponse:
    started = time.monotonic()
    library = folder = printer_share_id = "-"
    try:
        body = _body(req)
        library = _required_str(body, "library")
        folder = _required_folder(body)
        window_days = _tunable(print_policy.resolve_window_days, body.get("windowDays"))
        min_age_hours = _tunable(print_policy.resolve_min_age_hours,
                                 body.get("minAgeHours"))
        batch_size = _tunable(print_policy.resolve_batch_size, body.get("batchSize"))
        requested_share = (body.get("printerShareId") or "").strip()
        printer_share_id = requested_share or "-"

        client = _client()
        context = _resolve_list(client, library)

        # Outstanding == PRINT_PENDING or PRINT_FAILED. Two separate equality
        # queries rather than `ne PRINT_COMPLETED`: Graph filters one indexed
        # field at a time, `ne` on text is weakly supported, and the union is
        # exactly what the requirement names (docs/design.md R17).
        candidates: List[PrintFile] = []
        seen: set = set()
        for status in print_policy.RESUBMIT_STATUSES:
            for found in sharepoint.query_by_status(client, context, status, folder):
                # The two queries run at different instants, so a file whose
                # status changes between them appears in both. De-duplicate by
                # item id: the second pass would lose the eTag race anyway, but
                # it would still be counted twice in the response.
                if found.item_id in seen:
                    continue
                seen.add(found.item_id)
                candidates.append(found)

        now = print_policy.now_utc()
        eligible = [f for f in candidates
                    if print_policy.within_window(f.created, window_days, now)
                    and print_policy.older_than(f.created, min_age_hours, now)]

        budget = Budget(started=started)
        items: List[Dict[str, Any]] = []
        resubmitted = completed_instead = still_running = failed = cancelled = 0
        shares: Dict[str, universal_print.ShareInfo] = {}

        # Least recently ATTEMPTED first, not oldest-created. createdDateTime
        # never changes, so ordering a retry queue by it means a file that fails
        # every time is chosen every time and everything newer waits behind it
        # forever. Our own writes bump lastModifiedDateTime, so an attempt sends
        # a file to the back of the queue and the backlog rotates.
        for item in print_policy.select_least_recently_attempted(eligible, batch_size):
            if budget.exhausted:
                break

            share_id = requested_share or item.printer
            if not share_id:
                failed += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "failed",
                              "message": "no printer: the file has no Printer_Name "
                                         "and the request supplied no printerShareId"})
                continue

            share = _share_for(client, shares, share_id)
            if share is None:
                failed += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "failed",
                              "message": "printer share {!r} could not be "
                                         "resolved".format(share_id)})
                continue

            # Check the existing job before doing anything irreversible.
            if item.job_id:
                job = universal_print.get_job(client, item.printer or share_id,
                                              item.job_id)
                action = (print_policy.RESUBMIT_RESUBMIT if job is None
                          else print_policy.resubmit_action(
                              universal_print.job_state(job)))

                if action == print_policy.RESUBMIT_MARK_COMPLETED:
                    # It finished between Poll's last run and now. Reprinting
                    # would put a second copy in the tray.
                    #
                    # Same timestamp source Poll uses, deliberately: two code
                    # paths write this column, and if they disagreed the audit
                    # trail would say something different depending on which
                    # endpoint happened to get there first.
                    message = print_policy.printed_on_message(
                        print_policy.completion_time(
                            universal_print.job_acknowledged_at(job), now))
                    sharepoint.patch_fields(client, context, item.item_id, {
                        print_policy.COLUMN_STATUS: print_policy.COMPLETED,
                        print_policy.COLUMN_MESSAGE: message,
                    })
                    completed_instead += 1
                    items.append({"itemId": item.item_id, "fileName": item.file_name,
                                  "result": "completed_late", "jobId": item.job_id,
                                  "message": message})
                    _print_event(EP_RESUBMIT, item.item_id, from_status=item.status,
                                 to_status=print_policy.COMPLETED, job=item.job_id,
                                 printer=share_id, result="completed_late",
                                 file_name=item.file_name)
                    continue

                if action == print_policy.RESUBMIT_SKIP:
                    still_running += 1
                    items.append({"itemId": item.item_id, "fileName": item.file_name,
                                  "result": "still_running", "jobId": item.job_id,
                                  "message": universal_print.job_state(job)})
                    continue

                if action == print_policy.RESUBMIT_CANCEL_THEN_RESUBMIT:
                    # `stopped` means the printer needs attention BEFORE THE JOB
                    # CAN CONTINUE -- the job is alive. Without this cancel, the
                    # original and the replacement both print once someone clears
                    # the jam. Best-effort: a failed cancel is logged and we
                    # still resubmit, because a stuck document is worse.
                    # Cancel against the printer the job WAS submitted to,
                    # which is not necessarily the one we are about to use. When
                    # the request overrides the printer, aiming at the new one
                    # 404s -- and a 404 reads as "already gone" -- so this would
                    # report a clean cancel while the original stayed alive to
                    # print alongside its replacement.
                    origin = _share_for(client, shares, item.printer or share_id)
                    printer_id = origin.printer_id if origin else ""
                    if universal_print.cancel_job(client, printer_id, item.job_id):
                        cancelled += 1
                        _print_event(EP_RESUBMIT, item.item_id, job=item.job_id,
                                     printer=item.printer or share_id,
                                     result="cancelled", file_name=item.file_name)

            record = _submit_one(client, context, share, item, EP_RESUBMIT,
                                 result_name=RESULT_RESUBMITTED)
            items.append(record)
            if record["result"] == RESULT_RESUBMITTED:
                resubmitted += 1
            elif record["result"] == RESULT_FAILED:
                failed += 1

        _run_summary(EP_RESUBMIT, library=library, folder=folder,
                     printer=printer_share_id, found=len(eligible), ok=resubmitted,
                     failed=failed, skipped=still_running, remaining=-1,
                     http_status=200, started=started)
        return _json_response(200, {
            "library": library, "folder": folder, "windowDays": window_days,
            "minAgeHours": min_age_hours, "candidatesFound": len(eligible),
            "resubmitted": resubmitted, "completedInstead": completed_instead,
            "stillRunning": still_running, "cancelled": cancelled, "failed": failed,
            "budgetExhausted": budget.exhausted, "items": items,
        })

    except BadRequest as exc:
        _run_summary(EP_RESUBMIT, library=library, folder=folder,
                     printer=printer_share_id, http_status=400, started=started)
        return _json_response(400, {"error": str(exc)})
    except Exception as exc:
        return _server_error(EP_RESUBMIT, exc, library, folder, printer_share_id, started)


# --- shared error handling ----------------------------------------------------


def _server_error(endpoint: str, exc: Exception, library: str, folder: str,
                  printer: str, started: float) -> func.HttpResponse:
    """500 for anything unexpected.

    Two failures get a specific message because they have a specific fix and
    would otherwise cost an hour of diagnosis each: a dead refresh token (re-run
    the bootstrap script) and a missing column (fix the library).
    """
    if isinstance(exc, graph_auth.AuthBootstrapRequired):
        logging.error("delegated auth is broken: %s", exc)
        payload = {"error": str(exc), "remedy": "run scripts/bootstrap_token.py"}
    elif isinstance(exc, ColumnNotFound):
        logging.error("library schema problem: %s", exc)
        payload = {"error": str(exc),
                   "remedy": "add the missing column(s) to the SharePoint library"}
    else:
        logging.exception("%s failed", endpoint)
        payload = {"error": f"{type(exc).__name__}: {exc}"}

    _run_summary(endpoint, library=library, folder=folder, printer=printer,
                 http_status=500, started=started)
    return _json_response(500, payload)
