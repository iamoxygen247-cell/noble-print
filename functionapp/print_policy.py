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
# PRINT_READY is set by an upstream process, never by this app.
READY = "PRINT_READY"
PENDING = "PRINT_PENDING"
FAILED = "PRINT_FAILED"
COMPLETED = "PRINT_COMPLETED"

# The statuses Resubmit considers "outstanding". The requirement stated this two
# ways -- "PRINT_PENDING or PRINT_FAILED" and "NOT EQUAL to PRINT_COMPLETED" --
# which differ on PRINT_READY and on a blank status. The first reading is the one
# in force (docs/design.md R17): PRINT_READY belongs to Submit, and a blank status
# means the file was never queued at all.
RESUBMIT_STATUSES = (PENDING, FAILED)

# --- Tunables -----------------------------------------------------------------
# Each has a constant default, an env override, and range validation. An
# out-of-range REQUEST value is a 400; an out-of-range ENV value logs a warning
# and falls back, because a server misconfiguration must not fail every request.
DEFAULT_BATCH_SIZE = 5
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 15  # the requirement's "oldest 15" is the ceiling, not the default

DEFAULT_WINDOW_DAYS = 20
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 365

DEFAULT_MIN_AGE_HOURS = 72
MIN_MIN_AGE_HOURS = 1
MAX_MIN_AGE_HOURS = 8760  # one year

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

# --- Poll actions -------------------------------------------------------------
POLL_COMPLETE = "complete"   # write PRINT_COMPLETED + "printed on ..."
POLL_FAIL = "fail"           # write PRINT_FAILED + the job's own description
POLL_NONE = "none"           # still in flight -- write NOTHING


def poll_action(state: Optional[str]) -> str:
    """What Poll should do about a job in `state`.

    Only `completed` is in the written requirement. `canceled` and `aborted` are
    terminal too and will never print, so they are marked failed immediately
    rather than sitting in PRINT_PENDING for three days looking identical to a
    job that is merely queued (docs/design.md, agreed decisions).

    `stopped` is deliberately NOT a failure here: it means the printer needs
    attention but the job can still continue once it gets it.
    """
    normalized = (state or "").strip().lower()
    if normalized == JOB_COMPLETED:
        return POLL_COMPLETE
    if normalized in (JOB_CANCELED, JOB_ABORTED):
        return POLL_FAIL
    return POLL_NONE


# --- Resubmit actions ---------------------------------------------------------
RESUBMIT_MARK_COMPLETED = "mark_completed"      # finished between runs; do NOT reprint
RESUBMIT_SKIP = "skip"                          # genuinely in flight; leave alone
RESUBMIT_CANCEL_THEN_RESUBMIT = "cancel_then_resubmit"
RESUBMIT_RESUBMIT = "resubmit"                  # already terminal; nothing to cancel


def resubmit_action(state: Optional[str]) -> str:
    """What Resubmit should do about an existing job in `state`.

    The cancel-first branch is a correctness requirement, not tidiness. Graph
    defines `stopped` as "an issue with the printer needs to be addressed BEFORE
    THE JOB CAN CONTINUE" -- the job is alive. Creating a replacement without
    cancelling it means that when someone clears the paper jam, the original and
    the replacement BOTH print. `paused` and `unknown` get the same treatment
    because neither is documented as dead.

    `canceled` and `aborted` are already terminal, so there is nothing to cancel
    and a bare resubmit is safe. A 404 (job aged out of Universal Print) is the
    caller's concern and also resolves to a bare resubmit.
    """
    normalized = (state or "").strip().lower()
    if normalized == JOB_COMPLETED:
        return RESUBMIT_MARK_COMPLETED
    if normalized in (JOB_PENDING, JOB_PROCESSING):
        return RESUBMIT_SKIP
    if normalized in (JOB_CANCELED, JOB_ABORTED):
        return RESUBMIT_RESUBMIT
    # paused, stopped, unknown, and anything the service adds later.
    return RESUBMIT_CANCEL_THEN_RESUBMIT


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


def older_than(created: Optional[datetime], min_age_hours: int,
               now: Optional[datetime] = None) -> bool:
    """True if `created` is at least `min_age_hours` old. UTC throughout.

    The boundary is inclusive: a file created exactly 72 hours ago IS old enough,
    which keeps a job from waiting an extra scheduling cycle for a rounding
    difference.
    """
    if created is None:
        return False
    reference = now or now_utc()
    return (reference - created) >= timedelta(hours=min_age_hours)


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


def select_least_recently_attempted(items: Iterable[Any], batch_size: int) -> list:
    """The `batch_size` items least recently written to, oldest write first.

    Resubmit uses this instead of select_oldest, and the difference is not
    cosmetic. createdDateTime NEVER CHANGES, so ordering a retry queue by it
    means a file that fails every time is chosen every time -- and everything
    newer waits behind it forever. lastModifiedDateTime is bumped by our own
    writes, so attempting a file sends it to the back of the queue and the whole
    backlog rotates.

    Submit deliberately still orders by creation time: the requirement asks for
    "the oldest" files, and nothing there can loop.
    """
    return select_oldest(items, batch_size, key="modified")


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


def resolve_window_days(request_value: Any = None) -> int:
    return _resolve_int("windowDays", request_value, "PRINT_STATUS_WINDOW_DAYS",
                        DEFAULT_WINDOW_DAYS, MIN_WINDOW_DAYS, MAX_WINDOW_DAYS)


def resolve_min_age_hours(request_value: Any = None) -> int:
    return _resolve_int("minAgeHours", request_value, "PRINT_RESUBMIT_MIN_AGE_HOURS",
                        DEFAULT_MIN_AGE_HOURS, MIN_MIN_AGE_HOURS, MAX_MIN_AGE_HOURS)


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
