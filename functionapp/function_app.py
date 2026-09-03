"""
function_app.py — the three HTTP endpoints Power Automate calls.

    Health  POST /api/print/health   can this pipeline do any work right now?
              {printerShareId, printFormat?}
    Submit  POST /api/print/submit   claim PRINT_READY files that are DUE, create
                                     print jobs
              {sharepointHostname, sharepointSitePath, library, folder,
               printerShareId, batchSize?, printFormat?, dryRun?}
    Poll    POST /api/print/status   check PRINT_PENDING jobs; mark the finished,
                                     requeue the stalled, fail the hopeless
              {sharepointHostname, sharepointSitePath, library, folder,
               giveUpDays?, stallMinutes?, printerShareId?}

THE REQUEST CARRIES THE WHOLE ENVIRONMENT. The site used to come from app settings
and the tunables used to fall back to them; both now come from the flow body alone.
One Function App therefore serves any site a flow names, and everything that shaped
a run is visible in the flow that made it rather than split across two places with
the flow silently winning. The trade is in CLAUDE.md: whoever holds the function key
chooses the site, bounded only by what the service account can reach.

HEALTH RUNS FIRST AND WRITES NOTHING. Every failure this pipeline has was
otherwise discovered mid-run, several only after files had been claimed: an
offline printer is a Submit 200 with failed = 0 (S3); a dead refresh token 500s
everything; a pypdfium2 wheel that did not install is found one document at a
time, each row going PRINT_PENDING then PRINT_FAILED. One share read answers all
of them while the queue is untouched.

Everything marked `?` is optional and, where it is a threshold, resolves
request > app setting > default -- so the retry pacing, the batch size and the
upload format are all retuned by editing a Power Automate flow, with no deploy.

This module is a THIN ORCHESTRATOR. It sequences calls and shapes responses. It
contains no rules: every threshold, status string, window and message format
lives in print_policy, and every HTTP shape lives in the two adapters. If you
find yourself writing `if status == ...` here, it belongs in print_policy
(docs/design.md §5.2, §9).

THE ORDERING THAT MATTERS. Each file is CLAIMED -- an eTag-conditioned PATCH
setting PRINT_PENDING -- BEFORE its print job is created. The requirement writes
the status afterwards; this is a deliberate deviation, recorded at R5, because
the two orderings fail differently:

    claim first   a crash loses the print; Poll sees a row with no job, requeues
                  it on its next run, and stamps Print_Time with the next boundary
                  the file is owed. A file crashing on its FIRST attempt reprints
                  in 15-30 minutes; one already deep in its backoff waits out the
                  rest of that boundary, which is the intended behaviour rather
                  than a delay -- it has been failing for a while.
    claim last    a crash prints the document twice; nothing recovers that

A recoverable lost print beats a silent double print. The consequence, which
Poll must respect: a PRINT_PENDING row with an empty Print_JobId is normal, not
an error -- it is a crashed submission, and Poll requeues it.

RECOVERY LIVES IN POLL. There used to be a third endpoint, Resubmit, on a daily
flow that would not touch a file until it was 72 hours old. Poll now does that
work on its ten-minute cadence with an exponential schedule, so a jam at 09:00 is
retried by 09:10 rather than at 02:00 two nights later.

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
import printing
import printing.sender
import sharepoint
import universal_print
from graph_client import GraphClient
from sharepoint import ColumnNotFound, ListContext, PrintFile

app = func.FunctionApp()

EP_SUBMIT = "submit"
EP_POLL = "poll"
EP_HEALTH = "health"

# The share id is the PERISHABLE half of the share/printer pair: deleting and
# re-creating a share mints a new one while the printer id and its
# registeredDateTime are untouched. So every recorded copy -- README, the docs,
# live-printer-check.ps1's default, and the Flow bodies in Power Automate -- goes
# stale at that moment, and the only symptom is a 404. Named here once because
# both Health and _server_error hand it to whoever is reading.
STALE_SHARE_REMEDY = ("the printer share id may be stale -- read the current one "
                      "from Universal Print > Printers > the printer > Overview, "
                      "then update the Power Automate flows")


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


def _print_format(body: dict) -> str:
    """The upload format the caller is asking for, normalised, or "".

    "" means "decide from the printer's capabilities" -- the behaviour this
    pipeline had before the parameter existed, and still the default, so a flow
    that does not send `printFormat` is unaffected.

    An unknown format is a 400 rather than a fallback. The whole reason to name a
    format is to stop the app guessing, so silently guessing after a typo would
    defeat the parameter in exactly the case it was added for.
    """
    raw = body.get("printFormat")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise BadRequest("printFormat must be a string")
    if not raw.strip():
        return ""

    wanted = printing.normalize_format(raw)
    if wanted not in printing.SUPPORTED_PRINT_FORMATS:
        raise BadRequest(
            "printFormat {!r} is not supported (expected one of: {})".format(
                raw, ", ".join(printing.SUPPORTED_PRINT_FORMATS)))
    return wanted


def _optional_share_id(body: dict, key: str) -> str:
    """An optional printer share id. Absent or "" means "not supplied"."""
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BadRequest(f"{key} must be a string")
    return value.strip()


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


def _required_site_path(body: dict) -> str:
    """`sharepointSitePath` -- required key, but "" is a legitimate value.

    Exactly the shape `folder` has, and for the same reason: "" addresses the root
    site, which is a real answer, so an absent key cannot be read as a default. The
    caller has to say which site they mean, even when the answer is "the root one".
    """
    if "sharepointSitePath" not in body:
        raise BadRequest('sharepointSitePath is required (use "" or "/" for the '
                         'root site)')
    value = body.get("sharepointSitePath")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BadRequest("sharepointSitePath must be a string")
    return value.strip()


def _resolve_list(client: GraphClient, hostname: str, site_path: str,
                  library: str) -> ListContext:
    """Resolve the site and library, and with them the five column internal names.

    THE SITE COMES FROM THE REQUEST, NOT THE ENVIRONMENT. It used to be
    SHAREPOINT_HOSTNAME / SHAREPOINT_SITE_PATH in app settings, on the principle
    that config naming an environment should not be caller-supplied. That principle
    was traded deliberately: one Function App now serves any site a flow names,
    and every input to a run is visible in the flow that made it.

    The cost is real and is recorded in CLAUDE.md -- whoever holds the function key
    chooses the site, bounded only by what the delegated service account can reach.
    """
    site_id = sharepoint.resolve_site(client, hostname, site_path)
    return sharepoint.resolve_list(client, site_id, library)


def _share_for(client: GraphClient, cache: Dict[str, Any], share_id: str):
    """A printer share, cached per invocation, or None if it cannot be resolved.

    Returns None rather than raising because one of its callers is the
    best-effort cancel: a share we can no longer look up must not abort a
    requeue.
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


def _flag_possible_duplicate(entry: Dict[str, Any], item: PrintFile,
                             cancel_result: str, lingering_state: str) -> None:
    """Mark a response entry whose old job may still print, and log it.

    The row is about to lose its Print_JobId, so nothing looks at that job again;
    this is the last moment it can be named. `warning` is the field the live
    harness already surfaces (scripts/test.py prints it as `!! ...`), and the same
    field rule 1 uses for the unrecorded-job-id case.

    NOT written to Print_Message: the message says what the cancel achieved, which
    is fact. A lingering state is a snapshot of an asynchronous operation and can
    resolve itself a second later, so it belongs in the run's output and the log --
    where a human reads it now -- rather than in a permanent column.
    """
    warning = print_policy.duplicate_warning(item.job_id, cancel_result,
                                             lingering_state)
    if not warning:
        return
    entry["warning"] = warning
    logging.error("%s (item %s)", warning, item.item_id)


def _cancel_outstanding(client: GraphClient, cache: Dict[str, Any],
                        item: PrintFile, endpoint: str = EP_POLL,
                        printer: str = "") -> Tuple[str, str]:
    """Kill the job behind a row before its status is rewritten. Best-effort.

    `printer` is the share to cancel against, defaulting to the row's own
    Printer_Name. Poll passes its request-level printerShareId here when one was
    supplied -- see the override warning in the route, and defect F3.

    Both callers rewrite the row immediately afterwards -- one to PRINT_READY, one
    to PRINT_FAILED -- and in both cases leaving the job alive is a correctness
    bug, not untidiness (CLAUDE.md rule 2, defect D1):

        requeue   the original prints ALONGSIDE its replacement once someone
                  clears the jam
        give up   the abandoned job prints days later against a row that says
                  PRINT_FAILED

    Cancel is documented ONLY on /print/printers/{id}/jobs/{id}/cancel, so it
    needs the printer id behind the share, not the share id held in
    Printer_Name. The caller proceeds whatever comes back, because a stuck
    document is worse than a possible duplicate -- but it must now SAY which it
    got, instead of writing "cancelled" over both.

    Returns (cancel_result, lingering_state):

        cancel_result   one of print_policy.CANCEL_OK / CANCEL_FAILED /
                        CANCEL_NOTHING. CANCEL_NOTHING means there was no job in
                        the first place -- a crashed submission, rule 1 -- and is
                        the only one of the three that is not a worry. A job we
                        cannot ADDRESS (no share, no printer id) is CANCEL_FAILED,
                        not CANCEL_NOTHING: the job is alive and we just failed to
                        reach it, which is Health's NO_PRINTER_ID arriving too late
                        to help.

        lingering_state the job's own state read back AFTER an accepted cancel,
                        when that state is still not `canceled`. Empty otherwise.
                        Graph accepting the cancel is not the job dying --
                        observed live 2026-09-02, jobs 40/41 reached `canceled`
                        while 38/39 went on reading `stopped`, cause never
                        established -- and once the requeue clears
                        Print_JobId nothing ever looks at that job again. This is
                        the last chance to notice. It is an OBSERVATION, not a
                        verdict: cancel is asynchronous, so a job still reading
                        `stopped` here may yet settle.
    """
    share_id = printer or item.printer
    if not item.job_id:
        return print_policy.CANCEL_NOTHING, ""
    if not share_id:
        logging.warning("cannot cancel job %s: the row names no printer. "
                        "Continuing anyway -- a duplicate print is possible.",
                        item.job_id)
        return print_policy.CANCEL_FAILED, ""
    origin = _share_for(client, cache, share_id)
    printer_id = origin.printer_id if origin else ""
    cancelled = universal_print.cancel_job(client, printer_id, item.job_id)
    if not cancelled:
        return print_policy.CANCEL_FAILED, ""

    # A cancel is a per-file outcome in its own right, and this line is the
    # ONLY record that a particular job was killed -- Print_JobId is cleared
    # on the requeue that follows. `cancelled` is part of the closed result
    # vocabulary the weekly report counts (design.md §13).
    # The share the cancel was actually SENT to, which is not always the one
    # on the row -- an override makes them differ, and when a duplicate turns
    # up this line is what says which printer was addressed.
    _print_event(endpoint, item.item_id, from_status=item.status,
                 job=item.job_id, printer=share_id,
                 result="cancelled", file_name=item.file_name)

    # One extra GET, only on the path that just cancelled something. A job Graph
    # no longer has (None) is exactly what a cancel should produce, so that is
    # silence, not a finding.
    #
    # IT MUST NEVER RAISE, AND THE GUARD IS NOT DEFENSIVE HABIT. This sits between
    # the cancel and the PATCH to PRINT_READY, and nothing wraps the row loop --
    # an escape unwinds the whole route to a 500 with the row still PRINT_PENDING
    # and its job now `canceled`, so the NEXT run reads a terminal state and writes
    # PRINT_FAILED. That would spend a document to improve a log message. The read
    # is a diagnostic: when it cannot be done, the answer is simply no observation.
    try:
        after = universal_print.get_job(client, share_id, item.job_id)
    except Exception as exc:
        logging.warning("could not re-read job %s after cancelling it: %s. "
                        "The cancel was accepted; whether it took is unverified.",
                        item.job_id, exc)
        return print_policy.CANCEL_OK, ""
    state_after = universal_print.job_state(after) if after else ""
    if print_policy.cancel_confirmed(state_after):
        return print_policy.CANCEL_OK, ""
    return print_policy.CANCEL_OK, state_after


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


def _printer_report(share: universal_print.ShareInfo) -> Dict[str, Any]:
    """The printer block, in the one shape both the dry run and Health report.

    Extracted from Submit's dryRun branch when Health arrived. Two endpoints
    describing the same printer differently is the exact drift that made
    printing/plan.py wrong once already, and the cost of preventing it is this
    function. test_dryrun.py pins these key names.
    """
    return {
        "shareId": share.share_id,
        "printerId": share.printer_id,
        "displayName": share.display_name,
        "acceptingJobs": share.accepting_jobs,
        "state": share.state,
        "contentTypes": share.content_types,
        "dpis": share.dpis,
    }


def _converter_available() -> bool:
    """Whether the PDF renderer can actually be loaded.

    A module-level function for two reasons. pwg_converter imports pypdfium2
    LAZILY, inside convert_pdf, so a wheel that failed to install is otherwise
    discovered per file AFTER the row has been claimed -- PRINT_PENDING, then
    PRINT_FAILED, once per document. And it is the only seam a test can patch:
    the package is a hard requirement and is genuinely installed in the venv, so
    the failure cannot be provoked any other way.
    """
    try:
        import pypdfium2  # noqa: F401
    except Exception:
        logging.warning("pypdfium2 could not be imported; documents needing "
                        "conversion cannot be printed", exc_info=True)
        return False
    return True


def _dry_run_conversion(share: universal_print.ShareInfo,
                        print_format: str = "") -> Dict[str, Any]:
    """What would happen to a PDF on this printer, without submitting one.

    Reported by dryRun so a printer that needs rasterizing announces itself
    before anyone queues a file, rather than after every file has failed. When
    the caller named a `printFormat` this reports the profile THAT would run, not
    the one the capabilities imply -- otherwise the dry run would describe a
    different pipeline from the real one, which is the one thing it exists to
    rule out.
    """
    source = universal_print.DEFAULT_CONTENT_TYPE
    profile = printing.profile_for(share, source, print_format)
    if profile is None:
        return {"supported": False, "profile": None, "sourceContentType": source,
                "requestedFormat": print_format or None}

    report: Dict[str, Any] = {
        "supported": True,
        "profile": profile.name,
        "sourceContentType": source,
        "uploadContentType": profile.target_content_type(source),
        # None means "the printer's capabilities chose"; a string means the
        # caller did.
        "requestedFormat": print_format or None,
        "conversionRequired": profile.name != "passthrough",
    }
    try:
        report["jobConfiguration"] = profile.job_configuration(share)
    except Exception as exc:                                   # pragma: no cover
        report["jobConfiguration"] = None
        report["configurationError"] = str(exc)
    return report


# --- the shared per-file submission -------------------------------------------


RESULT_SUBMITTED = "submitted"
RESULT_FAILED = "failed"
RESULT_SKIPPED = "skipped"


def _submit_one(client: GraphClient, context: ListContext, share: universal_print.ShareInfo,
                item: PrintFile, endpoint: str,
                print_format: str = "") -> Dict[str, Any]:
    """Claim one file and put it on the printer.

    `print_format` is the caller's requested upload format, or "" to let the
    printer's capabilities decide. It reaches this function already validated
    against the share, so the only way it can fail here is per-DOCUMENT -- a file
    the chosen format cannot be produced from.

    Submit is now the only caller. It used to be shared verbatim with Resubmit,
    which is why a retry is indistinguishable from a first attempt here: Poll
    hands a stalled file back to PRINT_READY and Submit picks it up knowing
    nothing about its history. The retry count lives in the file's age, not in a
    flag threaded through this function.

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
    #
    # Print_JobId IS cleared: a stale id would send Poll looking up a job that
    # belongs to a previous attempt.
    #
    # Print_Message is NOT. It used to be, and that quietly defeated the retry
    # history: Poll appends "Job Id N cancelled. Retry job" on the way OUT of
    # PRINT_PENDING, and this claim is the very next write, so the entry was gone
    # within one Flow A cycle and the column could never hold more than one.
    # Nothing is lost by keeping it -- every terminal outcome REPLACES this column
    # ("printed on ..." on success, the error text on failure), so a stale entry is
    # visible only while the row is PRINT_PENDING, which is exactly when someone
    # asking "why is this taking so long?" wants to read it.
    # Print_Time IS cleared, and that half matters as much as the job id. The column
    # means "when the next attempt falls due", which is only a question worth asking
    # of a PRINT_READY row; leaving a schedule on a row that is printing right now
    # would be noise at best. The invariant it preserves -- Print_Time is only ever
    # set on PRINT_READY -- is what stops a stale future time surviving into a
    # terminal row and silently suppressing a human's reset (design §5.4).
    claimed = sharepoint.patch_fields(client, context, item.item_id, {
        print_policy.COLUMN_STATUS: print_policy.PENDING,
        print_policy.COLUMN_PRINTER: share_id,
        print_policy.COLUMN_JOB_ID: "",
        print_policy.COLUMN_PRINT_TIME: None,
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

        # A profile either sends the document as-is or converts it to something
        # this printer accepts. None means neither is possible -- and the two
        # ways of getting there need different messages, because they send
        # whoever reads Print_Message to different places:
        #
        #   no printFormat   the PRINTER cannot take this document at all
        #   printFormat      the printer is fine; THIS FILE cannot be turned into
        #                    the requested format (a .docx when pwg-raster was
        #                    asked for, say). The request was already checked
        #                    against the share at preflight, so the file is what
        #                    is wrong here, not the flow's configuration.
        profile = printing.profile_for(share, content_type, print_format)
        if profile is None:
            if print_format:
                raise universal_print.PrintStageError(
                    "content_type",
                    f"cannot produce {print_format} from a {content_type} document")
            raise universal_print.PrintStageError(
                "content_type",
                f"printer {share.display_name or share.share_id} does not accept "
                f"{content_type} (supports: {', '.join(share.content_types) or 'unknown'})")

        data = sharepoint.download(drive_item["download_url"])

        job_id = printing.sender.send(
            client, share, profile, data, file_name, content_type)

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
                print_policy.COLUMN_PRINT_TIME: None,
            })
        except Exception:
            # The file is left at PRINT_PENDING with no job id, which Poll reads
            # as a crashed submission and requeues. Losing the message is bad;
            # losing the file is worse.
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
    # which this design defines as a crashed submission (§5.3), so Poll requeues
    # it and the document prints a second time. That is exactly the silent double
    # print the claim-first ordering exists to prevent, reintroduced one line from
    # the end.
    #
    # MOVING RECOVERY INTO POLL MADE THIS WORSE, NOT BETTER. Resubmit would have
    # reprinted after 72 hours, which left a working day to notice. Poll requeues
    # on its very next run, and stamps the next boundary the file is owed -- which
    # on a first attempt has already passed, so Submit takes it at the next tick.
    # Measured at Flow A 15 min / Flow B 10 min: 15-30 minutes. The duplicate is in
    # the tray before anyone reads the log.
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
        # No number here on purpose. Submit does not know Poll's `stallMinutes` --
        # it is a Poll knob and arrives on Poll's request, not this one -- and it
        # cannot know how far into its backoff the file already is, which is what
        # actually decides when the reprint lands. This used to quote
        # DEFAULT_STALL_MINUTES as "roughly 5 minutes", which was wrong twice over:
        # the wrong knob, and measured recovery is 15-30 minutes on a first attempt
        # and longer for a file already retrying.
        warning = (f"possible duplicate print: job {job_id} was started but its id "
                   f"could not be written to SharePoint, so Poll will read this row "
                   f"as a crashed submission, requeue it, and the document will "
                   f"print a second time at its next retry boundary")
        logging.error("%s (item %s, printer %s)",
                      warning, item.item_id, share_id, exc_info=True)

    _print_event(endpoint, item.item_id, from_status=item.status,
                 to_status=print_policy.PENDING, job=job_id, printer=share_id,
                 result=RESULT_SUBMITTED, file_name=item.file_name,
                 ms=int((time.monotonic() - started) * 1000))
    record = {"itemId": item.item_id, "fileName": item.file_name,
              "result": RESULT_SUBMITTED, "jobId": job_id}
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
        # The site, checked AFTER the older inputs on purpose: placement only
        # decides which message a body missing several fields gets back, and
        # appending leaves every pre-existing 400 saying what it always said.
        hostname = _required_str(body, "sharepointHostname")
        site_path = _required_site_path(body)
        batch_size = _tunable(print_policy.resolve_batch_size, body.get("batchSize"))
        # "" means "let the printer's capabilities decide", which is what every
        # caller did before this parameter existed.
        print_format = _print_format(body)

        client = _client()
        context = _resolve_list(client, hostname, site_path, library)

        # Preflight BEFORE anything is claimed: an offline printer or an
        # unsupported document type must be discovered while the queue is
        # untouched, not after five files have been marked PRINT_PENDING.
        share = universal_print.get_share(client, printer_share_id)

        # A requested format the device does not report is a CALLER error, and
        # this is the last moment it can be reported as one for free -- the share
        # is already in hand and not a single row has been claimed. Checked ahead
        # of the accepting-jobs branch because a misconfigured flow will not fix
        # itself when the printer comes back, and answering "printer offline"
        # would hide it until it did.
        #
        # `supports` gives a printer that reports NO content types the benefit of
        # the doubt, so an under-reporting device is not blocked by this.
        if print_format and not share.supports(print_format):
            raise BadRequest(
                "printer {} does not accept {} (supports: {})".format(
                    share.display_name or printer_share_id, print_format,
                    ", ".join(share.content_types) or "unknown"))

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
                "printFormat": print_format or None,
                "candidatesFound": 0, "remainingReady": 0, "submitted": 0,
                "failed": 0, "skipped": 0, "notYetDue": 0,
                "budgetExhausted": False,
                # ...and the flow needs SOMETHING to notify on. This run is a 200
                # with failed = 0, which matches no error condition, so without an
                # explicit flag an offline printer is completely silent.
                "printerAvailable": False, "items": [],
                "message": f"printer share {share.display_name or printer_share_id} "
                           f"is not accepting jobs (state: {share.state or 'unknown'})",
            })

        all_ready = sharepoint.query_by_status(
            client, context, print_policy.READY, folder)
        # THE DUE FILTER RUNS BEFORE THE BATCH IS TAKEN, NEVER AFTER. select_oldest
        # sorts by creation time, and a file that has been requeued several times
        # has the OLDEST creation time in the queue -- so it sorts to the front and
        # is also the one most likely to be waiting on a future Print_Time. Filter
        # afterwards and a batch of five could be five not-yet-due files, doing no
        # work while genuinely due documents sat behind them.
        #
        # Filtered in Python, not in $filter: SharePoint honours one indexed field
        # at a time and Print_Status already holds that slot (sharepoint.py). The
        # rows have all been paged in regardless, so this costs nothing.
        now = print_policy.now_utc()
        candidates = [f for f in all_ready if print_policy.is_due(f.print_time, now)]
        not_yet_due = len(all_ready) - len(candidates)
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
                "printer": _printer_report(share),
                "printFormat": print_format or None,
                # Which profile would run, and what it would upload. This is how
                # you find out a printer needs conversion WITHOUT printing -- and,
                # with printFormat set, how you confirm the format you asked for
                # is the one that would actually be sent.
                "conversion": _dry_run_conversion(share, print_format),
                # candidatesFound counts what is DUE. A file waiting on a future
                # Print_Time is reported separately rather than folded in, so a dry
                # run against a queue that looks empty says which of the two empties
                # it is: nothing to print, or nothing due yet.
                "candidatesFound": len(candidates),
                "notYetDue": not_yet_due,
                "wouldSubmit": [{"itemId": f.item_id, "fileName": f.file_name,
                                 "created": f.created,
                                 "printTime": f.print_time} for f in selected],
            })

        budget = Budget(started=started)
        items: List[Dict[str, Any]] = []
        for item in selected:
            if budget.exhausted:
                logging.info("stopping after %s of %s files: wall-clock budget spent",
                             len(items), len(selected))
                break
            items.append(_submit_one(client, context, share, item, EP_SUBMIT,
                                     print_format))

        submitted = sum(1 for i in items if i["result"] == RESULT_SUBMITTED)
        failed = sum(1 for i in items if i["result"] == RESULT_FAILED)
        skipped = sum(1 for i in items if i["result"] == RESULT_SKIPPED)
        # Everything still PRINT_READY AND DUE: the candidates we did not take, plus
        # the ones we skipped because another run claimed them. The flow loops while
        # this is > 0, so it must never overcount or the Do-Until never ends -- which
        # is exactly why `candidates` is the due-only list and not-yet-due rows are
        # counted separately. A file waiting on a future Print_Time can never be
        # submitted this cycle, so including it here would spin the loop to its
        # iteration cap on every recurrence.
        remaining = max(0, len(candidates) - submitted - failed)

        _run_summary(EP_SUBMIT, library=library, folder=folder, printer=printer_share_id,
                     found=len(candidates), ok=submitted, failed=failed,
                     skipped=skipped, remaining=remaining, http_status=200,
                     started=started)
        return _json_response(200, {
            "library": library, "folder": folder, "printerShareId": printer_share_id,
            # Echoed so the App Insights trace records which format actually ran,
            # not whichever one the flow was believed to be sending. null means
            # the printer's capabilities chose.
            "printFormat": print_format or None,
            "batchSize": batch_size, "candidatesFound": len(candidates),
            "remainingReady": remaining, "submitted": submitted, "failed": failed,
            "skipped": skipped,
            # PRINT_READY rows held back by a future Print_Time. Deliberately NOT
            # part of remainingReady -- see there -- but reported, because "nothing
            # to print" and "nothing due yet" are different situations and a flow
            # that cannot tell them apart cannot say which one it is in.
            "notYetDue": not_yet_due,
            "budgetExhausted": budget.exhausted,
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
        hostname = _required_str(body, "sharepointHostname")
        site_path = _required_site_path(body)
        # Both knobs come from the flow body, then the built-in default. There is
        # no app-setting layer any more: the pacing of the whole retry schedule is
        # a number in Power Automate and nowhere else, so there is one place to
        # look when it is not what someone expected.
        #
        # `maxRetries` used to be here too. It bounded the NUMBER of requeues while
        # giveUpDays bounded the WAITING, which was two answers to one question --
        # the count is derived from the file's age anyway. A body still sending it
        # is accepted and ignored; there is no strict schema, and 400-ing would
        # break every deployed flow the moment this shipped.
        give_up_days = _tunable(print_policy.resolve_give_up_days,
                                body.get("giveUpDays"))
        stall_minutes = _tunable(print_policy.resolve_stall_minutes,
                                 body.get("stallMinutes"))
        # OPTIONAL, and a HARD OVERRIDE when supplied: every job lookup and every
        # cancel in this run addresses this share instead of the one named on the
        # row. Omit it and Poll behaves exactly as before, following each row's
        # own Printer_Name.
        #
        # THIS IS DEFECT F3's PRECONDITION, DELIBERATELY REINTRODUCED. Job ids are
        # per-printer, so if this share is not the one a row's job actually lives
        # on, the lookup 404s, the cancel 404s, a 404 reads as "already gone", and
        # the original job stays alive to print beside its replacement. That is
        # harmless while every flow names one printer -- the override then equals
        # Printer_Name and changes nothing -- and it is why the divergence is
        # counted and logged below rather than left silent.
        printer_override = _optional_share_id(body, "printerShareId")

        client = _client()
        context = _resolve_list(client, hostname, site_path, library)

        pending = sharepoint.query_by_status(
            client, context, print_policy.PENDING, folder)

        now = print_policy.now_utc()

        # EVERY pending row is considered -- there is no window pre-filter any
        # more. A file past the give-up threshold used to be dropped here and
        # touched by nothing ever again (docs/design.md G1); now it reaches the
        # loop and is failed explicitly, which is what closes that hole.
        #
        # No batch cap either. The requirement says "query the sharepoint folder
        # for ALL files ... with PRINT_STATUS = PRINT_PENDING", and unlike Submit
        # there is no loop in the Poll flow to collect a remainder. Capping it
        # also starved: taking the oldest N meant a handful of long-running jobs
        # occupied every slot run after run. Only the wall-clock budget bounds
        # this, and what it leaves behind is reported rather than dropped.
        ordered = print_policy.select_oldest(pending, len(pending))

        budget = Budget(started=started)
        items: List[Dict[str, Any]] = []
        completed = failed = still_running = not_found = malformed = 0
        requeued = gave_up = 0
        overridden = 0
        visited = 0
        shares: Dict[str, Any] = {}

        for item in ordered:
            if budget.exhausted:
                logging.info("stopping after %s of %s pending jobs: budget spent",
                             visited, len(ordered))
                break
            visited += 1

            # Which share this row's job is addressed on. The override wins
            # outright when it was supplied; otherwise the row's own column.
            printer = printer_override or item.printer

            # A row naming a DIFFERENT printer from the override is counted and
            # logged rather than absorbed. It cannot happen at all while the
            # flows name a single share.
            #
            # The COUNT is of configuration divergence -- every disagreeing row,
            # job or no job -- because that is the stable signal that the flow
            # and the library disagree, and it does not flicker as jobs come and
            # go. The WARNING is graded: only a row with an outstanding job can
            # actually print twice, so only that row gets told about F3. Saying
            # "may print alongside its replacement" about a row with no job at
            # all would be false, and a warning that cries wolf is one nobody
            # reads by the time it matters.
            if printer_override and item.printer and item.printer != printer_override:
                overridden += 1
                if item.job_id:
                    logging.warning(
                        "printerShareId override: item %s names printer %s but this "
                        "run addresses %s. Job ids are per-printer, so job %s may not "
                        "exist on the override, in which case it cannot be cancelled "
                        "and may print alongside its replacement (defect F3).",
                        item.item_id, item.printer, printer_override, item.job_id)
                else:
                    logging.warning(
                        "printerShareId override: item %s names printer %s but this "
                        "run addresses %s. No outstanding job on this row, so nothing "
                        "can print twice -- but the flow and the library disagree "
                        "about the printer, which is worth fixing.",
                        item.item_id, item.printer, printer_override)

            # A PRINT_PENDING row with no job id is a crashed submission, not an
            # error: Submit claimed it and died before creating the job (rule 1,
            # the deliberate cost of claiming first). Poll now OWNS this case --
            # it used to be left for Resubmit 72 h later. There is no job to
            # inspect, so it goes into poll_decision with no job and no job age,
            # which reads as stalled, and the schedule requeues it.
            job = None
            state = ""
            if item.job_id:
                if not printer:
                    malformed += 1
                    items.append({"itemId": item.item_id, "fileName": item.file_name,
                                  "result": "malformed",
                                  "message": "Print_JobId is set but Printer_Name is empty"})
                    continue

                job = universal_print.get_job(client, printer, item.job_id)
                if job is None:
                    # Universal Print no longer has it. Counted for visibility,
                    # then treated like any other dead attempt: unrecoverable, so
                    # the schedule decides whether to try again.
                    not_found += 1
                    _print_event(EP_POLL, item.item_id, from_status=item.status,
                                 job=item.job_id, printer=printer,
                                 result="not_found", file_name=item.file_name)
                else:
                    state = universal_print.job_state(job)

            # The current attempt's own clock. The job's createdDateTime where
            # there is a job; otherwise when we last wrote the row, which for a
            # crashed submission is the claim itself.
            job_created = print_policy.parse_graph_datetime(
                universal_print.job_created_at(job)) if job else None
            attempt_started = job_created or item.modified

            action, attempt = print_policy.poll_decision(
                state,
                file_created=item.created,
                has_job=job is not None,
                job_created=job_created,
                attempt_started=attempt_started,
                now=now,
                stall_minutes=stall_minutes,
                give_up_days=give_up_days,
            )

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
                    print_policy.COLUMN_PRINT_TIME: None,
                })
                completed += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "completed", "jobId": item.job_id,
                              "message": message})
                _print_event(EP_POLL, item.item_id, from_status=item.status,
                             to_status=print_policy.COMPLETED, job=item.job_id,
                             printer=printer, result="completed",
                             file_name=item.file_name)

            elif action == print_policy.POLL_FAIL:
                message = print_policy.truncate_message(
                    universal_print.job_description(job))
                sharepoint.patch_fields(client, context, item.item_id, {
                    print_policy.COLUMN_STATUS: print_policy.FAILED,
                    print_policy.COLUMN_MESSAGE: message,
                    print_policy.COLUMN_PRINT_TIME: None,
                })
                failed += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "failed_terminal", "jobId": item.job_id,
                              "message": message})
                _print_event(EP_POLL, item.item_id, from_status=item.status,
                             to_status=print_policy.FAILED, job=item.job_id,
                             printer=printer, result="failed_terminal",
                             file_name=item.file_name)

            elif action == print_policy.POLL_REQUEUE:
                # CANCEL BEFORE REQUEUING -- defect D1, and CLAUDE.md rule 2.
                # Graph defines `stopped` as "an issue with the printer needs to
                # be addressed BEFORE THE JOB CAN CONTINUE": the job is alive.
                # Leave it running and the original prints alongside the
                # replacement the moment somebody clears the jam. Best-effort: a
                # failed cancel still requeues, because a stuck document is the
                # worse outcome, but it is logged as the only warning a duplicate
                # may appear.
                #
                # Cancel is documented only on the PRINTER route, so it needs the
                # printer id behind the share -- not the share id in Printer_Name.
                cancel_result, lingering = _cancel_outstanding(
                    client, shares, item, printer=printer)

                message = print_policy.append_message(
                    item.message,
                    print_policy.requeue_message(item.job_id, attempt,
                                                 cancel_result))
                # WHEN THE NEXT ATTEMPT FALLS DUE -- the backoff, written down.
                # Clamped to the give-up deadline, so the last attempt always lands
                # inside the window rather than against a row that will already
                # have been failed. A time in the past means "print immediately",
                # which is the ordinary case for the early boundaries.
                due_at = print_policy.next_retry_time(
                    item.created, stall_minutes, attempt, give_up_days)
                # eTag-conditioned like the claim: two overlapping Poll runs must
                # not both cancel and both append.
                written = sharepoint.patch_fields(client, context, item.item_id, {
                    print_policy.COLUMN_STATUS: print_policy.READY,
                    print_policy.COLUMN_JOB_ID: "",
                    print_policy.COLUMN_MESSAGE: message,
                    print_policy.COLUMN_PRINT_TIME:
                        print_policy.format_business_datetime(due_at),
                }, etag=item.etag)
                if not written:
                    still_running += 1
                    items.append({"itemId": item.item_id, "fileName": item.file_name,
                                  "result": "skipped", "jobId": item.job_id,
                                  "message": "another run requeued this file first"})
                    continue

                requeued += 1
                entry = {"itemId": item.item_id, "fileName": item.file_name,
                         "result": "requeued", "jobId": item.job_id,
                         "retry": attempt, "printTime": due_at,
                         "message": message}
                _flag_possible_duplicate(entry, item, cancel_result, lingering)
                items.append(entry)
                _print_event(EP_POLL, item.item_id, from_status=item.status,
                             to_status=print_policy.READY, job=item.job_id,
                             printer=printer, result="requeued",
                             file_name=item.file_name)

            elif action == print_policy.POLL_GIVE_UP:
                # Cancel whatever is still outstanding FIRST. Without it the
                # abandoned job could print days later against a row that reads
                # PRINT_FAILED -- the column would be lying about paper that came
                # out of the tray.
                cancel_result, lingering = _cancel_outstanding(
                    client, shares, item, printer=printer)

                message = print_policy.append_message(
                    item.message,
                    print_policy.give_up_message(attempt, give_up_days,
                                                 cancel_result))
                sharepoint.patch_fields(client, context, item.item_id, {
                    print_policy.COLUMN_STATUS: print_policy.FAILED,
                    print_policy.COLUMN_MESSAGE: message,
                    print_policy.COLUMN_PRINT_TIME: None,
                })
                gave_up += 1
                entry = {"itemId": item.item_id, "fileName": item.file_name,
                         "result": "gave_up", "jobId": item.job_id,
                         "message": message}
                _flag_possible_duplicate(entry, item, cancel_result, lingering)
                items.append(entry)
                _print_event(EP_POLL, item.item_id, from_status=item.status,
                             to_status=print_policy.FAILED, job=item.job_id,
                             printer=printer, result="gave_up",
                             file_name=item.file_name)

            else:
                # One situation reaches here now: a healthy job in flight, inside
                # the stall threshold. There used to be two more -- a stalled job
                # inside the backoff gap, and one whose retries were spent -- and
                # both were rows quietly waiting while Poll declined them run after
                # run. Waiting is `Print_Time`'s job now, so a stalled row leaves
                # PRINT_PENDING immediately and does its waiting where it can be
                # seen.
                still_running += 1
                items.append({"itemId": item.item_id, "fileName": item.file_name,
                              "result": "still_running", "jobId": item.job_id,
                              "message": print_policy.still_running_message(
                                  state,
                                  print_policy.minutes_between(job_created, now),
                                  stall_minutes)})

        _run_summary(EP_POLL, library=library, folder=folder,
                     printer=printer_override or "-", found=len(ordered),
                     ok=completed, failed=failed + gave_up, skipped=still_running,
                     remaining=-1, http_status=200, started=started)
        return _json_response(200, {
            "library": library, "folder": folder,
            # Echoed back so a flow's own tuning is visible in the response and in
            # the App Insights trace, not just in whatever the flow meant to send.
            "giveUpDays": give_up_days, "stallMinutes": stall_minutes,
            # null means no override was sent and each row followed its own
            # Printer_Name.
            "printerShareId": printer_override or None,
            "checked": len(items), "completed": completed, "failed": failed,
            "requeued": requeued, "gaveUp": gave_up,
            "stillRunning": still_running, "notFound": not_found,
            "malformed": malformed, "budgetExhausted": budget.exhausted,
            # Rows whose Printer_Name disagreed with the override. NOT an error
            # count -- it is the double-print exposure this run carried, and it
            # should be 0 in a single-printer deployment. See defect F3.
            "printerOverridden": overridden,
            # These account for every pending row, exactly once (defect S5 -- a
            # crashed submission used to appear in no counter at all):
            #   checked + uncheckedCount == pendingFound
            "pendingFound": len(ordered),
            "uncheckedCount": len(ordered) - visited,
            "items": items,
        })

    except BadRequest as exc:
        _run_summary(EP_POLL, library=library, folder=folder,
                     http_status=400, started=started)
        return _json_response(400, {"error": str(exc)})
    except Exception as exc:
        return _server_error(EP_POLL, exc, library, folder, "-", started)


# --- Health -------------------------------------------------------------------


def _health_response(started: float, printer_share_id: str, print_format: str,
                     errors: List[Dict[str, Any]],
                     warnings: List[Dict[str, Any]],
                     printer: Optional[Dict[str, Any]] = None,
                     conversion: Optional[Dict[str, Any]] = None,
                     printer_name: str = "",
                     profile_name: str = "") -> func.HttpResponse:
    """One shape for every Health answer, healthy or not.

    ALWAYS a 200 when the check itself ran: 400 means the request was malformed
    and 500 means Health broke, so a non-200 never means "the printer is sick".
    That is deliberate. Power Automate marks a non-2xx HTTP action as FAILED,
    which halts the branch unless every downstream action carries a
    run-after override -- and the body, which is the entire diagnosis, becomes
    awkward to read at exactly the moment it matters.

    `healthy`, `errors` and `warnings` are present on every one of these, so a
    flow condition never needs a null check. That is the S3 lesson: an offline
    printer used to be a 200 that matched no condition at all.
    """
    healthy = not errors
    _run_summary(EP_HEALTH, printer=printer_share_id or "-",
                 ok=1 if healthy else 0, failed=len(errors),
                 skipped=len(warnings), http_status=200, started=started)
    return _json_response(200, {
        "healthy": healthy,
        "printerShareId": printer_share_id,
        "printFormat": print_format or None,
        "printer": printer,
        "conversion": conversion,
        "errors": errors,
        "warnings": warnings,
        "message": print_policy.health_message(errors, warnings,
                                               printer_name, profile_name),
    })


@app.route(route="print/health", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def check_printer_health(req: func.HttpRequest) -> func.HttpResponse:
    """Can this pipeline do any work right now? One Graph call, no writes.

    Called by Power Automate BEFORE Submit and Poll, so that the failures which
    are otherwise discovered mid-run -- an offline printer, a dead refresh token,
    a renderer that did not install -- are discovered while the queue is still
    untouched and nothing has been claimed.

    This route writes NOTHING, to SharePoint or to Universal Print. It is safe to
    call as often as a flow likes.
    """
    started = time.monotonic()
    printer_share_id = "-"
    print_format = ""
    try:
        body = _body(req)
        printer_share_id = _required_str(body, "printerShareId")
        # Reused verbatim from Submit, so an unknown format is a 400 here too. A
        # malformed REQUEST is not an unhealthy PRINTER, and conflating them would
        # have a flow notifying about a broken device over its own typo.
        print_format = _print_format(body)

        client = _client()

        # --- terminal stage --------------------------------------------------
        # Without a share there is nothing else to evaluate, so these answer with
        # one error and null blocks rather than a misleadingly partial picture.
        # The token provider is called lazily inside the first request, so a dead
        # refresh token surfaces HERE rather than from _client().
        try:
            share = universal_print.get_share(client, printer_share_id)
        except graph_auth.AuthBootstrapRequired as exc:
            return _health_response(
                started, printer_share_id, print_format,
                [print_policy.health_finding(
                    print_policy.HEALTH_AUTH_BOOTSTRAP_REQUIRED, str(exc),
                    "run scripts/bootstrap_token.py")], [])
        except graph_client.GraphError as exc:
            # A 404 is the perishable share id: deleting and re-creating a share
            # mints a new one while the printer id is untouched, so every recorded
            # copy -- including the Flow bodies -- goes stale at that moment.
            not_found = exc.status_code == 404
            return _health_response(
                started, printer_share_id, print_format,
                [print_policy.health_finding(
                    print_policy.HEALTH_PRINTER_NOT_FOUND if not_found
                    else print_policy.HEALTH_PRINTER_UNREACHABLE,
                    str(exc),
                    STALE_SHARE_REMEDY if not_found else "")], [])

        # --- accumulate every remaining finding ------------------------------
        # The conversion block comes from the SAME function the dry run uses, so
        # Health cannot describe a pipeline Submit would not run -- and the
        # profile, the conversion flag and any configuration error are read back
        # out of it rather than recomputed.
        conversion = _dry_run_conversion(share, print_format)
        profile_name = conversion.get("profile")
        conversion_required = bool(conversion.get("conversionRequired"))

        errors, warnings = print_policy.health_findings(
            accepting_jobs=share.accepting_jobs,
            state=share.state,
            content_types=share.content_types,
            printer_id=share.printer_id,
            print_format=print_format,
            # ShareInfo.supports owns "does this printer accept X" -- the same
            # call Submit's preflight makes. None means nothing was asked for.
            format_supported=share.supports(print_format) if print_format else None,
            profile_name=profile_name,
            conversion_required=conversion_required,
            # Only load the renderer when a profile would actually need it.
            converter_available=_converter_available() if conversion_required else True,
            configuration_error=conversion.get("configurationError") or "",
        )

        return _health_response(
            started, printer_share_id, print_format, errors, warnings,
            printer=_printer_report(share), conversion=conversion,
            printer_name=share.display_name or printer_share_id,
            profile_name=profile_name or "")

    except BadRequest as exc:
        _run_summary(EP_HEALTH, printer=printer_share_id,
                     http_status=400, started=started)
        return _json_response(400, {"error": str(exc)})
    except Exception as exc:
        return _server_error(EP_HEALTH, exc, "-", "-", printer_share_id, started)


# --- shared error handling ----------------------------------------------------


def _server_error(endpoint: str, exc: Exception, library: str, folder: str,
                  printer: str, started: float) -> func.HttpResponse:
    """500 for anything unexpected.

    Three failures get a specific message because they have a specific fix and
    would otherwise cost an hour of diagnosis each: a dead refresh token (re-run
    the bootstrap script), a missing column (fix the library), and a stale
    printer share id (read the current one and update the flows).

    The third was recorded as missing in docs/design.md long before it was
    written: a re-created share 404s on Submit's preflight, and without this the
    answer is a bare `GraphError: ... (HTTP 404)` that names no action. Health
    reports the same condition as PRINTER_NOT_FOUND; this is for whoever calls
    Submit without checking first.
    """
    if isinstance(exc, graph_auth.AuthBootstrapRequired):
        logging.error("delegated auth is broken: %s", exc)
        payload = {"error": str(exc), "remedy": "run scripts/bootstrap_token.py"}
    elif isinstance(exc, ColumnNotFound):
        logging.error("library schema problem: %s", exc)
        payload = {"error": str(exc),
                   "remedy": "add the missing column(s) to the SharePoint library"}
    elif (isinstance(exc, graph_client.GraphError) and exc.status_code == 404
            and "/print/shares/" in (exc.url or "")):
        # Scoped to the SHARE route on purpose. A 404 from a job lookup is
        # ordinary -- finished jobs age out, and get_job already absorbs it -- so
        # only the preflight's 404 earns this remedy.
        logging.error("printer share not found: %s", exc)
        payload = {"error": str(exc), "remedy": STALE_SHARE_REMEDY}
    else:
        logging.exception("%s failed", endpoint)
        payload = {"error": f"{type(exc).__name__}: {exc}"}

    _run_summary(endpoint, library=library, folder=folder, printer=printer,
                 http_status=500, started=started)
    return _json_response(500, payload)
