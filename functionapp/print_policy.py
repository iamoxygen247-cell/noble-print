"""
print_policy.py — the decision rules for the SharePoint → Universal Print pipeline.

STANDARD LIBRARY ONLY, BY CONTRACT. This module must never import a cloud SDK, an
HTTP client, or either adapter. That single constraint is what lets the whole rule
set be tested offline in well under a second with no Azure account, and it is what
makes the pipeline reusable: another workflow can keep the adapters and replace
only this file. A test asserts the constraint (tests/test_print_policy.py).

It is also the single source of truth for every constant in the system. If a
threshold, a status string, or a column name appears anywhere else in the app,
that is a bug.

Layering (docs/design.md §5.2):
    graph_auth / graph_client   transport, tokens        knows no domain
    sharepoint.py               Graph list/drive URLs    knows no print semantics
    universal_print.py          Graph print URLs         knows no SharePoint
    print_policy.py  <- here    the rules                knows no I/O
    function_app.py             the sequence only        knows no branching

TIME DISCIPLINE. Every window comparison (20 days, 72 hours) is done in UTC against
the SharePoint `createdDateTime`. The business time zone is used for DISPLAY ONLY,
in the "printed on ..." message. Mixing the two is how daylight-saving bugs get in,
so the two paths are deliberately separate functions and both are tested.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

POLICY_VERSION = "1.0"

# --- SharePoint columns -------------------------------------------------------
# DISPLAY names, as typed by the person who created the columns. The internal
# names Graph actually wants may differ (SharePoint encodes specials as _xHHHH_,
# so "Print_Status" may live as "Print_x005f_Status"), and they are resolved at
# runtime from the list's column definitions -- see sharepoint.resolve_columns.
# Never hardcode an internal name anywhere.
COLUMN_STATUS = "Print_Status"
COLUMN_JOB_ID = "Print_JobId"
COLUMN_MESSAGE = "Print_Message"
COLUMN_PRINTER = "Printer_Name"

COLUMN_DISPLAY_NAMES = (COLUMN_STATUS, COLUMN_JOB_ID, COLUMN_MESSAGE, COLUMN_PRINTER)

# --- Print status vocabulary --------------------------------------------------
# PRINT_READY is set by an upstream process AND by Poll when it requeues a stalled
# job. Everything else here is written only by this app.
READY = "PRINT_READY"
PENDING = "PRINT_PENDING"
FAILED = "PRINT_FAILED"
COMPLETED = "PRINT_COMPLETED"

# PRINT_FAILED is TERMINAL. Nothing retries it; a human resets the row. That is
# why the give-up message has to be worth reading -- it is the only account of why
# the document never printed.
#
# A status this app does not recognise (the live library uses NO_PRINT) is inert:
# no route queries it, so those rows are never picked up. That is by design; do
# not add handling for values the app does not own.

# --- Tunables -----------------------------------------------------------------
# Each has a constant default, an env override, and range validation. An
# out-of-range REQUEST value is a 400; an out-of-range ENV value logs a warning
# and falls back, because a server misconfiguration must not fail every request.
DEFAULT_BATCH_SIZE = 5
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 15  # the requirement's "oldest 15" is the ceiling, not the default

# How long a file may stay PRINT_PENDING before Poll cancels the outstanding job
# and writes PRINT_FAILED. Measured from the SharePoint creation time. This is the
# outer bound on WAITING; MAX_RETRIES is the bound on WORK.
DEFAULT_GIVE_UP_DAYS = 10
MIN_GIVE_UP_DAYS = 1
MAX_GIVE_UP_DAYS = 365

# How long a print job may sit without reaching a terminal state before it counts
# as stalled. Measured from printJob.createdDateTime -- the job's own clock, not
# ours, so a SharePoint edit cannot reset it. Doubles as the base of the retry
# backoff below, so one number rescales the whole schedule coherently.
DEFAULT_STALL_MINUTES = 5
MIN_STALL_MINUTES = 1
MAX_STALL_MINUTES = 1440  # one day

# The most requeues one file may receive. Each costs a render, a conversion and an
# upload, so this bounds the work an undeliverable document can consume.
DEFAULT_MAX_RETRIES = 10
MIN_MAX_RETRIES = 1
MAX_MAX_RETRIES = 20

BUSINESS_TZ = "America/Vancouver"
MESSAGE_MAX_CHARS = 255

# Universal Print job configuration. Deliberately minimal: the printer's own
# defaults decide duplex, colour and paper size. Passed straight through to
# POST /print/shares/{id}/jobs.
JOB_CONFIGURATION = {"copies": 1}

# The whole-invocation wall-clock budget. Must undercut Power Automate's ~120 s
# HTTP connector budget so the structured response reaches the flow before the
# connector gives up. Checked BEFORE starting each file, never mid-file, so the
# cap can never strand a claimed-but-unsubmitted row.
DEFAULT_BUDGET_SECONDS = 90.0

# --- Universal Print job states ----------------------------------------------
# The eight documented values of printJobStatus.state.
JOB_UNKNOWN = "unknown"
JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_PAUSED = "paused"
JOB_STOPPED = "stopped"
JOB_COMPLETED = "completed"
JOB_CANCELED = "canceled"
JOB_ABORTED = "aborted"

ALL_JOB_STATES = (
    JOB_UNKNOWN, JOB_PENDING, JOB_PROCESSING, JOB_PAUSED,
    JOB_STOPPED, JOB_COMPLETED, JOB_CANCELED, JOB_ABORTED,
)

# States that will never make progress on their own.
TERMINAL_JOB_STATES = (JOB_COMPLETED, JOB_CANCELED, JOB_ABORTED)

# --- Universal Print PRINTER states -------------------------------------------
# printerProcessingState, on the SHARE. A DIFFERENT ENUM from the job states
# above, and the trap is that both spell "stopped".
#
#   JOB_STOPPED         one print job is blocked; the job CAN still continue
#   PRINTER_STATE_STOPPED   the device itself reports a fault
#
# Never substitute one for the other, and never compare a job state to a printer
# state. test_health.py pins that they stay distinct constants.
PRINTER_STATE_UNKNOWN = "unknown"
PRINTER_STATE_IDLE = "idle"
PRINTER_STATE_PROCESSING = "processing"
PRINTER_STATE_STOPPED = "stopped"

ALL_PRINTER_STATES = (PRINTER_STATE_UNKNOWN, PRINTER_STATE_IDLE,
                      PRINTER_STATE_PROCESSING, PRINTER_STATE_STOPPED)

# --- Poll actions -------------------------------------------------------------
POLL_COMPLETE = "complete"   # write PRINT_COMPLETED + "printed on ..."
POLL_FAIL = "fail"           # write PRINT_FAILED + the job's own description
POLL_NONE = "none"           # nothing to do -- write NOTHING
POLL_REQUEUE = "requeue"     # cancel the job, then write PRINT_READY
POLL_GIVE_UP = "give_up"     # cancel the job, then write PRINT_FAILED


def job_is_stalled(state: Optional[str], has_job: bool,
                   job_age_minutes: Optional[float], stall_minutes: int) -> bool:
    """True if this attempt has stopped making progress.

    Two shapes of "stuck" and one deliberate abstention:

    * NO JOB (`has_job` false) -- either a crashed submission, the PRINT_PENDING
      row with an empty Print_JobId that claiming-before-printing deliberately
      creates (rule 1), or a job Universal Print has purged. Nothing is coming;
      stalled immediately, with no age to wait on.
    * A LIVE JOB past the threshold in a non-terminal state.
    * A live job whose age CANNOT BE DETERMINED is NOT stalled. Universal Print
      returns createdDateTime, but if a job ever arrives without one, guessing
      "stalled" would cancel a job that might be printing right now and queue a
      second copy. Abstaining costs a delayed retry; guessing costs a duplicate.

    A terminal state is never stalled -- the caller resolves those first.
    """
    normalized = (state or "").strip().lower()
    if normalized in TERMINAL_JOB_STATES:
        return False
    if not has_job:
        return True
    if job_age_minutes is None:
        return False
    return job_age_minutes >= stall_minutes


def retries_due(file_age_minutes: Optional[float], base_minutes: int,
                max_retries: int) -> int:
    """How many requeues the schedule says should have happened by this age.

    Retry n falls due at `base * (2**n - 1)`: 5, 15, 35, 75, 155 ... minutes for
    the default base of 5. Exponential, so a printer that is off for the weekend
    is retried a handful of times rather than every ten minutes for sixty hours.

    THIS IS WHAT MAKES THE RETRY COUNT STATELESS. The four-column schema has no
    attempt counter and none was added; instead the count is a pure function of
    age, and comparing this value at two instants tells the caller whether a new
    retry has come due. See `poll_decision`.

    Capped at `max_retries` so the arithmetic cannot run away on an ancient row.
    """
    if file_age_minutes is None or file_age_minutes < base_minutes:
        return 0
    count = 0
    while count < max_retries:
        if file_age_minutes < base_minutes * (2 ** (count + 1) - 1):
            break
        count += 1
    return count


def minutes_between(earlier: Optional[datetime],
                    later: Optional[datetime]) -> Optional[float]:
    """Elapsed minutes, or None if either end is unknown.

    None propagates deliberately: an age that cannot be computed must not be
    silently treated as zero, which would read as "brand new" and suppress every
    retry the row was owed.
    """
    if earlier is None or later is None:
        return None
    return (later - earlier).total_seconds() / 60.0


def poll_decision(state: Optional[str], *,
                  file_created: Optional[datetime],
                  has_job: bool,
                  job_created: Optional[datetime],
                  attempt_started: Optional[datetime],
                  now: datetime,
                  stall_minutes: int, give_up_days: int,
                  max_retries: int) -> tuple:
    """(action, retry_number) for one PRINT_PENDING row.

    `attempt_started` is when the CURRENT attempt began -- the print job's own
    createdDateTime, or the row's lastModifiedDateTime when there is no job.

    THE RETRIES ALREADY SPENT ARE READ OFF THE CLOCK, NOT A COUNTER: they are
    `retries_due` evaluated at the FILE'S AGE WHEN THIS ATTEMPT STARTED. Every
    requeue creates a fresh job, so an attempt that began late in the schedule
    proves the earlier retries happened. Note this is the file's age at that
    moment, NOT the attempt's own age -- those coincide on the first attempt, and
    using the latter would make `spent` equal `due` forever and no retry would
    ever fire.

    THE ORDER OF THESE CHECKS IS LOAD-BEARING:

    1. `completed` wins over everything, INCLUDING the give-up deadline. A job
       that finished a minute past the cut-off still put paper in the tray, and a
       row reading PRINT_FAILED about a document that printed is worse than a late
       success.
    2. `canceled` / `aborted` are terminal and will never print.
    3. Past the give-up threshold: stop waiting. The caller cancels whatever is
       outstanding FIRST -- without that, an abandoned job could print days later
       against a row that says PRINT_FAILED.
    4. Stalled, retries left, and a new boundary crossed: requeue.
    5. Anything else: leave it alone. That covers three situations which all want
       silence -- a healthy job in flight, a stalled job inside the backoff gap,
       and a stalled job whose retries are spent but which is still inside the
       give-up window. The last is the GRACE PERIOD: no further work is spent, but
       the final job stays live, so a printer that comes back still gets the
       document out and check 1 records it.

    A row whose creation time will not parse is left alone entirely. It cannot be
    aged, so neither the give-up test nor the schedule means anything for it, and
    guessing would either abandon a live document or retry one forever.
    """
    normalized = (state or "").strip().lower()
    if normalized == JOB_COMPLETED:
        return POLL_COMPLETE, 0
    if normalized in (JOB_CANCELED, JOB_ABORTED):
        return POLL_FAIL, 0

    spent = retries_due(minutes_between(file_created, attempt_started),
                        stall_minutes, max_retries)

    if file_created is None:
        return POLL_NONE, spent
    if not within_window(file_created, give_up_days, now):
        return POLL_GIVE_UP, spent

    if not job_is_stalled(state, has_job, minutes_between(job_created, now),
                          stall_minutes):
        return POLL_NONE, spent
    if spent >= max_retries:
        return POLL_NONE, spent

    due = retries_due(minutes_between(file_created, now), stall_minutes, max_retries)
    if due > spent:
        return POLL_REQUEUE, due
    return POLL_NONE, spent


# --- Printer health -----------------------------------------------------------
# The codes the Health endpoint reports. They are a CLOSED, STABLE vocabulary: a
# Power Automate condition and a KQL query both key on them, so renaming one is a
# breaking change even though nothing in Python would fail.
HEALTH_AUTH_BOOTSTRAP_REQUIRED = "AUTH_BOOTSTRAP_REQUIRED"
HEALTH_PRINTER_NOT_FOUND = "PRINTER_NOT_FOUND"
HEALTH_PRINTER_UNREACHABLE = "PRINTER_UNREACHABLE"
HEALTH_PRINTER_NOT_ACCEPTING_JOBS = "PRINTER_NOT_ACCEPTING_JOBS"
HEALTH_PRINTER_STOPPED = "PRINTER_STOPPED"
HEALTH_NO_PRINTER_ID = "NO_PRINTER_ID"
HEALTH_FORMAT_NOT_SUPPORTED = "FORMAT_NOT_SUPPORTED"
HEALTH_NO_PROFILE = "NO_PROFILE"
HEALTH_JOB_CONFIGURATION_FAILED = "JOB_CONFIGURATION_FAILED"
HEALTH_CONVERTER_UNAVAILABLE = "CONVERTER_UNAVAILABLE"

HEALTH_NO_CONTENT_TYPES = "NO_CONTENT_TYPES"
HEALTH_PRINTER_STATE_UNKNOWN = "PRINTER_STATE_UNKNOWN"

# Every code this module can emit, errors and warnings alike. Used by the docs and
# by a test that they are unique -- two constants sharing a string would make one
# of them permanently unreportable.
ALL_HEALTH_CODES = (
    HEALTH_AUTH_BOOTSTRAP_REQUIRED, HEALTH_PRINTER_NOT_FOUND,
    HEALTH_PRINTER_UNREACHABLE, HEALTH_PRINTER_NOT_ACCEPTING_JOBS,
    HEALTH_PRINTER_STOPPED, HEALTH_NO_PRINTER_ID,
    HEALTH_FORMAT_NOT_SUPPORTED, HEALTH_NO_PROFILE,
    HEALTH_JOB_CONFIGURATION_FAILED, HEALTH_CONVERTER_UNAVAILABLE,
    HEALTH_NO_CONTENT_TYPES, HEALTH_PRINTER_STATE_UNKNOWN,
)


def health_finding(code: str, message: str, remedy: str = "") -> dict:
    """One finding. `remedy` is omitted entirely rather than sent as an empty
    string, so a flow can test for its presence."""
    finding = {"code": code, "message": message}
    if remedy:
        finding["remedy"] = remedy
    return finding


def health_findings(*, accepting_jobs: bool, state: Optional[str],
                    content_types: Sequence[Any], printer_id: str,
                    print_format: str, format_supported: Optional[bool],
                    profile_name: Optional[str],
                    conversion_required: bool,
                    converter_available: bool,
                    configuration_error: str = "") -> tuple:
    """(errors, warnings) for one printer share. Pure -- primitives only.

    Called once, AFTER the share has been read. Everything that makes the share
    unreadable at all -- a dead token, a 404, an unreachable service -- is decided
    by the caller and short-circuits before this function is reached, because
    without a share there is nothing here to evaluate.

    EVERY finding is collected; nothing short-circuits. A flow that is told one
    problem per ten-minute cycle takes an hour to learn about six.

    `format_supported` IS PASSED IN RATHER THAN COMPUTED HERE, and that is
    deliberate. "Does this printer accept X" already has exactly one definition --
    universal_print.ShareInfo.supports -- and Submit's preflight uses it. A second
    implementation in this module would be free to disagree with the endpoint it
    is supposed to be predicting, which is the whole failure mode Health exists to
    prevent. None means the caller named no format, so there is nothing to check.
    """
    errors = []
    warnings = []
    normalized_state = (state or "").strip().lower()

    # -- can the service take work at all? ------------------------------------
    if not accepting_jobs:
        errors.append(health_finding(
            HEALTH_PRINTER_NOT_ACCEPTING_JOBS,
            "the printer share is not accepting jobs "
            f"(state: {normalized_state or 'unknown'})",
            "wake or reconnect the printer, then re-run this check"))

    if normalized_state == PRINTER_STATE_STOPPED:
        # Note this is printerProcessingState, NOT printJobStatus.state -- see the
        # constants above. Treated as fatal by explicit decision: a stopped device
        # may still queue work that prints on recovery, so this deliberately stops
        # Flow A rather than letting documents pile up against a faulty printer.
        errors.append(health_finding(
            HEALTH_PRINTER_STOPPED,
            "the printer reports a fault (printerProcessingState: stopped)",
            "clear the fault at the device -- paper, toner, covers, jams"))
    elif normalized_state in ("", PRINTER_STATE_UNKNOWN):
        warnings.append(health_finding(
            HEALTH_PRINTER_STATE_UNKNOWN,
            "the printer does not report a processing state; "
            "it cannot be checked, only tried"))

    # -- can a stalled job be cancelled later? --------------------------------
    if not printer_id:
        # Cancel is documented ONLY on /print/printers/{id}/jobs/{id}/cancel. With
        # no printer id behind the share, Poll cannot cancel a stalled job before
        # requeuing it, and the original prints alongside its replacement (rule 2,
        # defect D1). Nothing else in the app checks this.
        errors.append(health_finding(
            HEALTH_NO_PRINTER_ID,
            "the share reports no printer id, so a stalled job could not be "
            "cancelled before being retried -- risking a duplicate print",
            "re-create the printer share, then update the flows with the new "
            "share id"))

    # -- can we produce what this printer takes? ------------------------------
    if not content_types:
        # ShareInfo.supports gives an under-reporting device the benefit of the
        # doubt, so this is a warning: we cannot verify the format, and the app
        # will attempt it anyway rather than refuse to print.
        warnings.append(health_finding(
            HEALTH_NO_CONTENT_TYPES,
            "the printer reports no content types, so the upload format cannot "
            "be verified in advance"))

    if format_supported is False:
        offered = ", ".join(str(t) for t in content_types) or "unknown"
        errors.append(health_finding(
            HEALTH_FORMAT_NOT_SUPPORTED,
            f"the printer does not accept {print_format} (supports: {offered})",
            "send a printFormat the printer reports, or omit it and let the "
            "capabilities choose"))

    # `not profile_name` rather than `is None`: the caller reads this out of the
    # conversion report, where "no profile" is None today -- but an empty name
    # would sail through an identity check and report a healthy printer with no
    # way to print, which is the worst answer this endpoint could give.
    if not profile_name:
        errors.append(health_finding(
            HEALTH_NO_PROFILE,
            "no conversion profile can produce anything this printer accepts "
            "from a PDF"))

    if configuration_error:
        errors.append(health_finding(
            HEALTH_JOB_CONFIGURATION_FAILED,
            f"the job configuration for this printer could not be built: "
            f"{truncate_message(configuration_error)}"))

    # -- can we run the conversion the profile needs? -------------------------
    # Only when a conversion is actually required. A passthrough printer never
    # loads the renderer, so a missing one is not its problem.
    if conversion_required and not converter_available:
        errors.append(health_finding(
            HEALTH_CONVERTER_UNAVAILABLE,
            "the PDF renderer is not installed, so every document needing "
            "conversion would fail after being claimed",
            'python -m pip install "pypdfium2>=5,<6" and redeploy'))

    return errors, warnings


def health_message(errors: Sequence[Any], warnings: Sequence[Any],
                   printer_name: str = "", profile_name: str = "") -> str:
    """One line a human can read without opening the arrays."""
    if errors:
        codes = ", ".join(str(e.get("code", "?")) for e in errors)
        plural = "problem" if len(errors) == 1 else "problems"
        return f"{len(errors)} {plural}: {codes}"

    ready = f"printer {printer_name or 'share'} is ready"
    if profile_name:
        ready += f"; documents go through the {profile_name} profile"
    if warnings:
        codes = ", ".join(str(w.get("code", "?")) for w in warnings)
        plural = "warning" if len(warnings) == 1 else "warnings"
        ready += f" ({len(warnings)} {plural}: {codes})"
    return ready


# --- Time ---------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_graph_datetime(value: Any) -> Optional[datetime]:
    """Parse a Graph ISO-8601 timestamp into an aware UTC datetime.

    Graph returns `2026-08-30T14:23:23Z`; `fromisoformat` did not accept the `Z`
    suffix before Python 3.11, so it is normalised first. A naive value is
    assumed UTC, which is what Graph documents. Returns None on anything
    unparseable, so a single malformed row cannot fail a whole batch.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def within_window(created: Optional[datetime], window_days: int,
                  now: Optional[datetime] = None) -> bool:
    """True if `created` falls inside the trailing window. UTC throughout.

    A row with no parseable creation time is EXCLUDED: it cannot be shown to be
    in scope, and silently including it would let an unbounded set through.
    """
    if created is None:
        return False
    reference = now or now_utc()
    return created >= reference - timedelta(days=window_days)


# `older_than` used to live here: Resubmit would not touch a file until it was 72
# hours old. Poll's exponential schedule replaced that single coarse gate, so the
# helper went with the endpoint. `minutes_between` + `retries_due` cover the same
# ground at every timescale from five minutes upward.


def _business_zone(tz_name: Optional[str] = None):
    """The display time zone. Falls back to UTC with a warning if the host has no
    IANA database -- `tzdata` is in requirements.txt precisely so this does not
    happen, but a reporting time zone must never be able to fail a print run."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = tz_name or os.getenv("PRINT_BUSINESS_TZ") or BUSINESS_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logging.warning("time zone %r unavailable; formatting timestamps in UTC", name)
        return timezone.utc


PRINTED_ON_FORMAT = "%Y-%m-%d %H:%M:%S"


def completion_time(acknowledged: Any, observed: Optional[datetime] = None) -> datetime:
    """The best available answer to "when was this printed?".

    `acknowledged` is printJob.acknowledgedDateTime, documented only as "the
    dateTimeOffset when the job was acknowledged". `observed` is the moment Poll
    looked. Prefer the first, because the two are not equally good:

        acknowledged   a property of the JOB. On the job measured 2026-08-30 it
                       landed 26 s after creation and ~10 s before the page
                       finished. Stable -- re-reading gives the same answer.
        observed       a property of OUR CRON. Poll runs every ten minutes, so
                       this can be most of an interval late, and it changes if
                       somebody edits the schedule.

    Neither is the true completion instant -- printJob has no such field, which
    is why the message still says "printed on" rather than claiming precision it
    does not have (docs/design.md R11). But one of them is about the printer and
    the other is about us.

    Falls back rather than raising: a missing or unparseable timestamp must never
    cost us the status write, or the file sits in PRINT_PENDING forever over a
    cosmetic field.
    """
    return parse_graph_datetime(acknowledged) or observed or now_utc()


def printed_on_message(when: Optional[datetime] = None,
                       tz_name: Optional[str] = None) -> str:
    """The Print_Message written when a job completes, e.g.
    "printed on 2026-08-01 14:23:23", rendered in the business time zone.

    `when` should come from completion_time(): the printer's own
    acknowledgedDateTime where there is one, else the moment Poll observed the
    job finished. Neither is the instant the page left the tray -- printJob has
    no completion field, and inventing one would be a silent default -- so the
    message says "printed on" and claims no more than that. The README says so in
    as many words, because someone will eventually ask.
    """
    moment = (when or now_utc()).astimezone(_business_zone(tz_name))
    return f"printed on {moment.strftime(PRINTED_ON_FORMAT)}"


# --- Messages -----------------------------------------------------------------


def truncate_message(text: Any, limit: int = MESSAGE_MAX_CHARS) -> str:
    """Fit an error into Print_Message. A long Graph error must never be the
    reason the PATCH that records the failure itself fails."""
    if text is None:
        return ""
    flattened = " ".join(str(text).split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: max(0, limit - 1)] + "…"


def failure_message(stage: str, error: Any) -> str:
    """The Print_Message for a failed submission, tagged with the stage that failed.

    The requirement says to record "the error message returned by Universal Print
    service", but a submission can also fail while reading the file out of
    SharePoint. Reporting that as a printer fault would send whoever reads the
    column to the wrong system, so the stage is named explicitly.
    """
    detail = truncate_message(error, MESSAGE_MAX_CHARS - len(stage) - 2)
    return truncate_message(f"{stage}: {detail}" if detail else f"{stage}: failed")


MESSAGE_SEPARATOR = " | "


def append_message(existing: Any, addition: Any,
                   limit: int = MESSAGE_MAX_CHARS) -> str:
    """Add to Print_Message instead of replacing it, keeping the NEWEST entries.

    The retry history is the one thing in this system a person can read to see
    what happened to a document, so a requeue adds to it rather than overwriting
    the reason the previous attempt failed.

    When the column's 255 characters run out the OLDEST entries are dropped, not
    the newest: the most recent attempt is what someone is looking at the row to
    understand. Roughly six or seven entries fit.

    Nothing reads this back for control flow -- `retries_due` derives the attempt
    count from the file's age -- so truncation can never change behaviour. It is
    for humans only.
    """
    tail = truncate_message(addition, limit)
    head = " ".join(str(existing or "").split())
    if not head:
        return tail
    if not tail:
        return truncate_message(head, limit)

    combined = head + MESSAGE_SEPARATOR + tail
    if len(combined) <= limit:
        return combined

    # Drop whole entries off the front until it fits, so the column never holds
    # half a message. The newest entry alone always survives.
    parts = combined.split(MESSAGE_SEPARATOR)
    while len(parts) > 1 and len(MESSAGE_SEPARATOR.join(parts)) > limit:
        parts.pop(0)
    return truncate_message(MESSAGE_SEPARATOR.join(parts), limit)


def requeue_message(job_id: Any, attempt: int) -> str:
    """The entry appended when Poll cancels a stalled job and requeues the file."""
    label = str(job_id or "").strip() or "(none)"
    return f"Job Id {label} cancelled. Retry job ({attempt})"


def give_up_message(retries: int, give_up_days: int) -> str:
    """The entry appended when Poll stops trying.

    This is the last thing written to the row and PRINT_FAILED is terminal, so it
    has to say enough for someone to act: how many retries were spent and how long
    was allowed. `retries` counts REQUEUES, not attempts -- 0 is legitimate and
    means the original submission was the only one, which is what a row that was
    never seen to stall looks like.
    """
    return (f"gave up after {give_up_days} day(s) and {retries} retr"
            f"{'y' if retries == 1 else 'ies'}; outstanding job cancelled")


# --- Selection ----------------------------------------------------------------


def select_oldest(items: Iterable[Any], batch_size: int,
                  key: str = "created") -> list:
    """The oldest `batch_size` items, oldest first.

    Done here rather than in the Graph query on purpose: `$orderby` on `fields/*`
    is not documented as supported for SharePoint list items and is widely
    reported to fail, and only one indexed field may be filtered at a time. So
    the query filters on status alone and the ordering happens in Python.

    Ties are broken by the item id so the order is deterministic -- two files
    created in the same second must not swap places between runs, or a test that
    asserts "which five" becomes a coin flip.
    """
    def sort_key(item):
        created = getattr(item, key, None) if not isinstance(item, dict) else item.get(key)
        # None sorts last: an unparseable creation time should not jump the queue.
        return (created is None, created or datetime.max.replace(tzinfo=timezone.utc),
                str(_item_id(item)))

    return sorted(items, key=sort_key)[:batch_size]


# `select_least_recently_attempted` used to live here, ordering Resubmit's retry
# queue by lastModifiedDateTime so a chronically failing file could not monopolise
# every run (defect F2). It went with the Resubmit endpoint: Poll has no batch
# cap -- it visits every row in the window each run -- so that starvation cannot
# occur and the rotation has nothing left to fix.


def _item_id(item: Any) -> Any:
    if isinstance(item, dict):
        return item.get("id", "")
    return getattr(item, "id", "")


# --- Tunable resolution -------------------------------------------------------


def _resolve_int(name: str, request_value: Any, env_var: str,
                 default: int, low: int, high: int) -> int:
    """Resolve a tunable from (request > env > constant) with range validation.

    A bad REQUEST value raises ValueError, which the route turns into a 400: the
    caller asked for something impossible and should be told. A bad ENV value
    logs a warning and falls back, because server misconfiguration must not take
    every request down with it.
    """
    if request_value is not None:
        try:
            value = int(request_value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer, got {request_value!r}")
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}, got {value}")
        return value

    raw = os.getenv(env_var)
    if raw:
        try:
            value = int(raw)
            if low <= value <= high:
                return value
            logging.warning("%s=%s is outside [%s, %s]; using %s",
                            env_var, raw, low, high, default)
        except ValueError:
            logging.warning("%s=%s is not an integer; using %s", env_var, raw, default)
    return default


def resolve_batch_size(request_value: Any = None) -> int:
    return _resolve_int("batchSize", request_value, "PRINT_BATCH_SIZE",
                        DEFAULT_BATCH_SIZE, MIN_BATCH_SIZE, MAX_BATCH_SIZE)


# The three retry knobs below are read from the POWER AUTOMATE REQUEST BODY first,
# then the app setting, then the default. That ordering is the point: the pacing
# of the whole retry schedule can be retuned by editing a flow, with no deploy and
# no app-setting change.
def resolve_give_up_days(request_value: Any = None) -> int:
    return _resolve_int("giveUpDays", request_value, "PRINT_GIVE_UP_DAYS",
                        DEFAULT_GIVE_UP_DAYS, MIN_GIVE_UP_DAYS, MAX_GIVE_UP_DAYS)


def resolve_stall_minutes(request_value: Any = None) -> int:
    return _resolve_int("stallMinutes", request_value, "PRINT_STALL_MINUTES",
                        DEFAULT_STALL_MINUTES, MIN_STALL_MINUTES, MAX_STALL_MINUTES)


def resolve_max_retries(request_value: Any = None) -> int:
    return _resolve_int("maxRetries", request_value, "PRINT_MAX_RETRIES",
                        DEFAULT_MAX_RETRIES, MIN_MAX_RETRIES, MAX_MAX_RETRIES)


def resolve_budget_seconds() -> float:
    raw = os.getenv("PRINT_BUDGET_SECONDS")
    if raw:
        try:
            value = float(raw)
            if 0 < value <= 600:
                return value
            logging.warning("PRINT_BUDGET_SECONDS=%s is outside (0, 600]; using %s",
                            raw, DEFAULT_BUDGET_SECONDS)
        except ValueError:
            logging.warning("PRINT_BUDGET_SECONDS=%s is not a number; using %s",
                            raw, DEFAULT_BUDGET_SECONDS)
    return DEFAULT_BUDGET_SECONDS


# --- Folder scoping -----------------------------------------------------------


def normalize_folder(folder: Optional[str]) -> str:
    """Normalise a folder input to a comparable form: no leading or trailing
    slash, forward slashes only. "" and "/" both mean the library root."""
    if not folder:
        return ""
    return str(folder).replace("\\", "/").strip("/")


def folder_matches(item_folder: Optional[str], target_folder: str) -> bool:
    """True if a file living in `item_folder` is in scope for `target_folder`.

    `item_folder` is whatever SharePoint gives us -- typically the server-relative
    FileDirRef, e.g. "/sites/Operations/Shared Documents/Invoices/ToPrint" --
    while the caller supplies something short and human, e.g. "/Invoices/ToPrint".
    Rather than trying to reconstruct and strip the site + library prefix (which
    differs between a library's title and its URL segment, and breaks the moment
    someone renames one), the target is matched as a contiguous run of path
    segments anywhere in the item's path.

    That makes "Invoices/ToPrint" match the folder itself and everything beneath
    it, and an empty target -- a caller who omits the folder -- match the whole
    library. Comparison is case-insensitive because SharePoint paths are.
    """
    target_parts = [p.lower() for p in normalize_folder(target_folder).split("/") if p]
    if not target_parts:
        return True
    item_parts = [p.lower() for p in normalize_folder(item_folder).split("/") if p]
    span = len(target_parts)
    return any(item_parts[i:i + span] == target_parts
               for i in range(len(item_parts) - span + 1))
