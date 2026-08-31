"""
test_resubmit.py — Tier C: the Resubmit endpoint.

Resubmit is the endpoint most able to cause harm: everything it does results in
another sheet of paper. Three assertions here carry that weight:

* test_a_stopped_job_is_cancelled_before_the_replacement -- without the cancel,
  the original and the replacement both print once the printer is fixed.
* test_a_job_that_completed_late_is_not_reprinted -- a job that finished between
  Poll's last run and now must be recorded, not repeated.
* test_ready_and_blank_files_are_never_resubmitted -- pins the reading of a
  requirement that stated its scope two different ways (docs/design.md R17).
"""

from __future__ import annotations

import logging

import pytest

import print_policy
from helpers import RESUBMIT, as_json, iso, post, print_events, result_for

BODY = {"library": "Documents", "folder": "/Invoices/ToPrint",
        "printerShareId": "share-guid"}


def body(**overrides):
    merged = dict(BODY)
    merged.update(overrides)
    return merged


def outstanding(graph, item_id="1", *, status=print_policy.PENDING,
                job_id="1825", hours_old=100, printer=None, **kwargs):
    """A file old enough to be eligible: past 72h, inside 20 days."""
    graph.add_item(item_id, status=status, job_id=job_id,
                   printer=graph.SHARE_ID if printer is None else printer,
                   created=iso(hours=hours_old), **kwargs)


# --- the cancel-first guard ---------------------------------------------------


def test_a_stopped_job_is_cancelled_before_the_replacement(graph, frozen_now):
    """THE double-print guard.

    Graph defines `stopped` as "an issue with the printer needs to be addressed
    BEFORE THE JOB CAN CONTINUE" -- the job is alive. Creating a replacement
    without cancelling means that when someone clears the paper jam, the original
    AND the replacement both come out.
    """
    outstanding(graph, job_id="1825")
    graph.add_job("1825", state="stopped", description="Out of paper")

    payload = as_json(post(RESUBMIT, body()))

    assert graph.cancelled == ["1825"], "the stalled job must be cancelled"
    assert payload["cancelled"] == 1
    assert payload["resubmitted"] == 1

    cancel_index = next(i for i, c in enumerate(graph.calls) if "/cancel" in c.url)
    create_index = next(i for i, c in enumerate(graph.calls)
                        if c.method == "POST" and c.url.endswith("/jobs"))
    assert cancel_index < create_index, "cancel must precede the replacement job"


def test_the_cancel_uses_the_printer_id_from_the_share(graph, frozen_now):
    """Cancel is documented only on /print/printers/{id}/..., so it needs the
    PRINTER id while everything else uses the SHARE id."""
    outstanding(graph, job_id="1825")
    graph.add_job("1825", state="stopped")

    post(RESUBMIT, body())

    cancel_url = graph.calls_to("/cancel", method="POST")[0].url
    assert "/print/printers/{}/jobs/1825/cancel".format(graph.PRINTER_ID) in cancel_url


@pytest.mark.parametrize("state", ["paused", "unknown"])
def test_other_non_terminal_states_are_also_cancelled(graph, frozen_now, state):
    outstanding(graph, job_id="1825")
    graph.add_job("1825", state=state)

    post(RESUBMIT, body())

    assert graph.cancelled == ["1825"]


@pytest.mark.parametrize("state", ["canceled", "aborted"])
def test_an_already_terminal_job_is_not_cancelled_again(graph, frozen_now, state):
    """Nothing to cancel: the job is already dead. A pointless call would just be
    one more thing to fail."""
    outstanding(graph, job_id="1825", status=print_policy.FAILED)
    graph.add_job("1825", state=state)

    payload = as_json(post(RESUBMIT, body()))

    assert graph.cancelled == []
    assert payload["resubmitted"] == 1


def test_a_failed_cancel_still_resubmits(graph, frozen_now):
    """Best-effort by contract. A stuck document is worse than a possible
    duplicate, so we proceed -- but the failure is logged, because that log line
    is the only warning that a duplicate may appear."""
    outstanding(graph, job_id="1825")
    graph.add_job("1825", state="stopped")
    graph.fail_next("POST", "/cancel", status=500, times=3)

    payload = as_json(post(RESUBMIT, body()))

    assert payload["cancelled"] == 0
    assert payload["resubmitted"] == 1
    assert graph.field("1", "Print_Status") == print_policy.PENDING


# --- not reprinting what already printed --------------------------------------


def test_a_job_that_completed_late_is_not_reprinted(graph, frozen_now):
    """It finished between Poll's last run and now. Reprinting would put a second
    copy in the tray for no reason."""
    outstanding(graph, job_id="1825")
    graph.add_job("1825", state="completed")

    payload = as_json(post(RESUBMIT, body()))

    assert payload["completedInstead"] == 1
    assert payload["resubmitted"] == 0
    assert graph.field("1", "Print_Status") == print_policy.COMPLETED
    assert graph.field("1", "Print_Message").startswith("printed on ")
    assert not [c for c in graph.calls
                if c.method == "POST" and c.url.endswith("/jobs")]


@pytest.mark.parametrize("state", ["pending", "processing"])
def test_a_job_still_in_flight_is_left_alone(graph, frozen_now, state):
    """It is queued behind other work, not stuck. Cancelling and resubmitting
    would just move it to the back of the same queue."""
    outstanding(graph, job_id="1825")
    graph.add_job("1825", state=state)

    payload = as_json(post(RESUBMIT, body()))

    assert payload["stillRunning"] == 1
    assert payload["resubmitted"] == 0
    assert graph.cancelled == []
    assert graph.field("1", "Print_Status") == print_policy.PENDING


# --- recovering a crashed submission -----------------------------------------


def test_a_claim_crash_row_is_resubmitted(graph, frozen_now):
    """UC-6: PRINT_PENDING with no job id -- Submit claimed it and died. There is
    nothing to cancel, and this is exactly the case claim-before-submit trades
    for; Resubmit is the recovery."""
    outstanding(graph, job_id="")

    payload = as_json(post(RESUBMIT, body()))

    assert payload["resubmitted"] == 1
    assert graph.cancelled == []
    assert graph.field("1", "Print_JobId") != ""
    assert graph.field("1", "Print_Status") == print_policy.PENDING


def test_a_job_purged_from_universal_print_is_resubmitted(graph, frozen_now):
    outstanding(graph, job_id="9999")  # no such job registered

    payload = as_json(post(RESUBMIT, body()))

    assert payload["resubmitted"] == 1
    assert graph.field("1", "Print_JobId") not in ("", "9999")


# --- scope: the R17 decision --------------------------------------------------


def test_ready_and_blank_files_are_never_resubmitted(graph, frozen_now):
    """The requirement stated the scope twice -- "PRINT_PENDING or PRINT_FAILED"
    and "NOT EQUAL to PRINT_COMPLETED" -- which differ on PRINT_READY and blank.
    The first reading is in force: PRINT_READY is Submit's territory, and a blank
    status means the file was never queued.
    """
    graph.add_item("ready", status=print_policy.READY, created=iso(hours=200))
    graph.add_item("blank", status="", created=iso(hours=200))
    graph.add_item("done", status=print_policy.COMPLETED, created=iso(hours=200))
    outstanding(graph, "pending", job_id="")

    payload = as_json(post(RESUBMIT, body()))

    assert [i["itemId"] for i in payload["items"]] == ["pending"]
    assert graph.status_of("ready") == print_policy.READY
    assert graph.status_of("blank") == ""
    assert graph.status_of("done") == print_policy.COMPLETED


def test_both_pending_and_failed_are_in_scope(graph, frozen_now):
    outstanding(graph, "pending", status=print_policy.PENDING, job_id="")
    outstanding(graph, "failed", status=print_policy.FAILED, job_id="")

    payload = as_json(post(RESUBMIT, body()))

    assert payload["resubmitted"] == 2
    assert {i["itemId"] for i in payload["items"]} == {"pending", "failed"}


# --- scope: the time windows --------------------------------------------------


def test_a_file_younger_than_72_hours_is_left_alone(graph, frozen_now):
    outstanding(graph, "young", job_id="", hours_old=71)
    outstanding(graph, "old", job_id="", hours_old=73)

    payload = as_json(post(RESUBMIT, body()))

    assert [i["itemId"] for i in payload["items"]] == ["old"]
    assert graph.field("young", "Print_JobId") == ""


def test_exactly_72_hours_is_eligible(graph, frozen_now):
    """An exclusive boundary would make a file wait a whole extra scheduling
    cycle for a rounding difference."""
    outstanding(graph, "exact", job_id="", hours_old=72)

    assert as_json(post(RESUBMIT, body()))["resubmitted"] == 1


def test_a_file_older_than_20_days_is_out_of_scope(graph, frozen_now):
    """G1, the requirement's own cliff: past the window a file is invisible to
    both Poll and Resubmit. Asserted so the behaviour is deliberate and visible,
    not an accident someone discovers later."""
    outstanding(graph, "stale", job_id="", hours_old=21 * 24)

    payload = as_json(post(RESUBMIT, body()))

    assert payload["candidatesFound"] == 0
    assert graph.field("stale", "Print_JobId") == ""


def test_the_windows_are_configurable(graph, frozen_now):
    outstanding(graph, "1", job_id="", hours_old=30)

    assert as_json(post(RESUBMIT, body()))["resubmitted"] == 0
    assert as_json(post(RESUBMIT, body(minAgeHours=24)))["resubmitted"] == 1


def test_takes_the_oldest_first_within_the_batch(graph, frozen_now):
    outstanding(graph, "newest", job_id="", hours_old=80)
    outstanding(graph, "oldest", job_id="", hours_old=300)
    outstanding(graph, "middle", job_id="", hours_old=150)

    payload = as_json(post(RESUBMIT, body(batchSize=2)))

    assert [i["itemId"] for i in payload["items"]] == ["oldest", "middle"]


# --- printer resolution -------------------------------------------------------


def test_the_printer_falls_back_to_the_files_own_column(graph, frozen_now):
    """A resubmit without an explicit printer should go back to the same device
    the file was originally sent to."""
    outstanding(graph, "1", job_id="", printer=graph.SHARE_ID)

    payload = as_json(post(RESUBMIT, {"library": "Documents",
                                      "folder": "/Invoices/ToPrint"}))

    assert payload["resubmitted"] == 1
    assert graph.field("1", "Printer_Name") == graph.SHARE_ID


def test_a_request_printer_overrides_the_files_column(graph, frozen_now):
    outstanding(graph, "1", job_id="", printer="some-old-share")

    payload = as_json(post(RESUBMIT, body()))

    assert payload["resubmitted"] == 1
    assert graph.field("1", "Printer_Name") == graph.SHARE_ID


def test_a_file_with_no_printer_at_all_fails_with_a_clear_message(graph, frozen_now):
    outstanding(graph, "1", job_id="", printer="")

    payload = as_json(post(RESUBMIT, {"library": "Documents",
                                      "folder": "/Invoices/ToPrint"}))

    assert payload["failed"] == 1
    assert "no printer" in result_for(payload, "1")["message"]


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize("payload,fragment", [
    ({}, "library"),
    ({"library": "Documents"}, "folder"),
])
def test_a_malformed_request_is_400(graph, payload, fragment):
    response = post(RESUBMIT, payload)
    assert response.status_code == 400
    assert fragment in as_json(response)["error"]


def test_a_bad_min_age_is_400(graph):
    assert post(RESUBMIT, body(minAgeHours=0)).status_code == 400


def test_a_400_never_touches_the_queue(graph, frozen_now):
    outstanding(graph, "1", job_id="")
    post(RESUBMIT, {"library": "Documents"})

    assert graph.field("1", "Print_JobId") == ""
    assert not [c for c in graph.calls if c.method == "PATCH"]


# --- telemetry ----------------------------------------------------------------


def test_a_resubmission_is_distinguishable_from_a_first_submission(graph,
                                                                   frozen_now, caplog):
    """The weekly report counts retries separately from submissions. If both
    emitted result=submitted, "how many were retried" would be unanswerable."""
    caplog.set_level(logging.INFO)
    outstanding(graph, "1", job_id="", name="invoice.pdf")

    post(RESUBMIT, body())

    events = print_events(caplog)
    assert any(e["ep"] == "resubmit" and e["result"] == "resubmitted"
               for e in events), events


def test_a_late_completion_is_reported_as_completed_late(graph, frozen_now, caplog):
    caplog.set_level(logging.INFO)
    outstanding(graph, "1", job_id="1825")
    graph.add_job("1825", state="completed")

    post(RESUBMIT, body())

    event = print_events(caplog)[0]
    assert event["result"] == "completed_late"
    assert event["to"] == "PRINT_COMPLETED"
