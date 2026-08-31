"""
test_poll.py — Tier C: the Poll endpoint.

Poll's job is narrow and its restraint is the point: it writes only for a
terminal job state. Most of this file asserts what Poll does NOT do, because a
premature write here marks a document printed that may never have printed at all.
"""

from __future__ import annotations

import logging
import re

import pytest

import print_policy
from helpers import POLL, as_json, iso, post, print_events, result_for

BODY = {"library": "Documents", "folder": "/Invoices/ToPrint"}

PRINTED_ON = re.compile(r"^printed on \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def body(**overrides):
    merged = dict(BODY)
    merged.update(overrides)
    return merged


def pending_item(graph, item_id="1", *, job_id="1825", days_old=1, **kwargs):
    graph.add_item(item_id, status=print_policy.PENDING, job_id=job_id,
                   printer=graph.SHARE_ID, created=iso(days=days_old), **kwargs)


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


def test_a_stopped_job_is_left_for_resubmit_not_failed(graph, frozen_now):
    """`stopped` means the printer needs attention but the job can still
    continue. Marking it failed here would invite a reprint of a job that is
    about to print by itself."""
    pending_item(graph)
    graph.add_job("1825", state="stopped", description="Out of paper")

    payload = as_json(post(POLL, body()))

    assert payload["stillRunning"] == 1
    assert graph.field("1", "Print_Status") == print_policy.PENDING


def test_a_job_purged_from_universal_print_writes_nothing(graph, frozen_now):
    """A 404 means the job aged out. Assuming "it must have printed" would mark
    documents complete that may never have printed; Resubmit handles it at 72h."""
    pending_item(graph, job_id="9999")  # no matching job registered

    payload = as_json(post(POLL, body()))

    assert payload["notFound"] == 1
    assert payload["completed"] == 0
    assert graph.field("1", "Print_Status") == print_policy.PENDING
    assert not graph.calls_to("/items/1/fields", method="PATCH")


def test_a_pending_row_with_no_job_id_is_skipped(graph, frozen_now):
    """UC-6: Submit claimed the file and crashed before creating the job. There
    is nothing to poll. This is NORMAL, not an error -- it is the cost of
    claiming before submitting, and Resubmit recovers it."""
    graph.add_item("1", status=print_policy.PENDING, job_id="",
                   printer=graph.SHARE_ID, created=iso(days=1))

    payload = as_json(post(POLL, body()))

    assert payload["checked"] == 0
    assert payload["completed"] == 0
    assert payload["notFound"] == 0
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


def test_the_twenty_day_window_excludes_older_files(graph, frozen_now):
    pending_item(graph, "recent", job_id="1825", days_old=19)
    pending_item(graph, "stale", job_id="1826", days_old=25)
    graph.add_job("1825", state="completed")
    graph.add_job("1826", state="completed")

    payload = as_json(post(POLL, body()))

    assert [i["itemId"] for i in payload["items"]] == ["recent"]
    assert graph.field("stale", "Print_Status") == print_policy.PENDING


def test_the_window_is_configurable(graph, frozen_now):
    pending_item(graph, "old", job_id="1825", days_old=25)
    graph.add_job("1825", state="completed")

    payload = as_json(post(POLL, body(windowDays=30)))

    assert payload["completed"] == 1


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


def test_a_bad_window_is_400(graph):
    assert post(POLL, body(windowDays=0)).status_code == 400
    assert post(POLL, body(windowDays=9999)).status_code == 400


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
