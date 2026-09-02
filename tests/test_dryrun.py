"""
test_dryrun.py — the operator-facing dry run, and the death of the 20-day cliff.

Both exist because of specific failure modes that are otherwise invisible:

* dryRun answers "will this work here?" before a single sheet of paper is
  committed. It resolves the same site, library, columns and printer the real
  run uses, so a wrong column name or an offline printer surfaces in the
  runbook step rather than in the tray.

* G1 -- the 20-day cliff -- used to need a `staleCount` field to surface it: a
  file past the window was excluded by BOTH Poll and Resubmit, so nothing would
  ever touch it again and nothing would ever say so. THAT DEFECT IS FIXED, not
  merely reported. Poll now examines every pending row and writes PRINT_FAILED
  past the give-up threshold, so there is nothing left to strand and no
  `staleCount` to publish. The tests that pinned the reporting are replaced below
  by ones that pin the fix.
"""

from __future__ import annotations

import print_policy
from helpers import POLL, SITE, SUBMIT, as_json, iso, post

SUBMIT_BODY = {**SITE, "library": "Documents", "folder": "/Invoices/ToPrint",
               "printerShareId": "share-guid"}
POLL_BODY = {**SITE, "library": "Documents", "folder": "/Invoices/ToPrint"}


# --- dry run ------------------------------------------------------------------


def test_dry_run_prints_nothing_and_claims_nothing(graph):
    graph.add_item("1")

    payload = as_json(post(SUBMIT, dict(SUBMIT_BODY, dryRun=True)))

    assert payload["dryRun"] is True
    assert graph.status_of("1") == print_policy.READY
    assert not [c for c in graph.calls if c.method == "PATCH"]
    assert not [c for c in graph.calls
                if c.method == "POST" and c.url.endswith("/jobs")]


def test_dry_run_reports_the_resolved_internal_column_names(graph):
    """The single most useful thing it can tell you. SharePoint fixes internal
    names at creation and may encode them, so seeing the real mapping is how you
    confirm the library is wired the way the code expects."""
    graph.add_item("1")

    payload = as_json(post(SUBMIT, dict(SUBMIT_BODY, dryRun=True)))

    assert payload["resolvedColumns"] == {
        "Print_Status": "Print_x005f_Status",
        "Print_JobId": "Print_x005f_JobId",
        "Print_Message": "Print_x005f_Message",
        "Printer_Name": "Printer_x005f_Name",
        "Print_Time": "Print_x005f_Time",
    }


def test_dry_run_reports_the_printer_and_its_capabilities(graph):
    graph.add_item("1")

    printer = as_json(post(SUBMIT, dict(SUBMIT_BODY, dryRun=True)))["printer"]

    assert printer["shareId"] == graph.SHARE_ID
    assert printer["printerId"] == graph.PRINTER_ID
    assert printer["acceptingJobs"] is True
    assert "application/pdf" in printer["contentTypes"]


def test_dry_run_lists_exactly_what_a_real_run_would_take(graph):
    for i in range(9):
        graph.add_item(str(i), created="2026-08-0{}T10:00:00Z".format(i + 1))

    payload = as_json(post(SUBMIT, dict(SUBMIT_BODY, dryRun=True)))

    assert payload["candidatesFound"] == 9
    assert [f["itemId"] for f in payload["wouldSubmit"]] == ["0", "1", "2", "3", "4"]


def test_dry_run_still_validates_the_request(graph):
    """A dry run that accepts a bad request would give false confidence."""
    assert post(SUBMIT, {"library": "Documents", "dryRun": True}).status_code == 400


def test_dry_run_still_surfaces_a_missing_column(graph):
    """The whole point is to fail here rather than in production."""
    graph.drop_columns = ["Print_JobId"]
    graph.add_item("1")

    response = post(SUBMIT, dict(SUBMIT_BODY, dryRun=True))

    assert response.status_code == 500
    assert "Print_JobId" in as_json(response)["error"]


# --- G1: stranding is now impossible ------------------------------------------


def test_a_file_past_the_give_up_threshold_is_failed_not_stranded(graph, frozen_now):
    """G1/UC-10 closed. This row used to be dropped from Poll's window and
    excluded from Resubmit's, so it sat at PRINT_PENDING forever with nothing to
    signal it. It is now given a terminal answer and a reason."""
    graph.add_item("fresh", status=print_policy.PENDING, job_id="1825",
                   printer=graph.SHARE_ID, created=iso(days=2))
    graph.add_item("stranded", status=print_policy.PENDING, job_id="1826",
                   printer=graph.SHARE_ID, created=iso(days=40))
    graph.add_job("1825", state="processing", created=iso(minutes=1))
    graph.add_job("1826", state="stopped", created=iso(days=40))

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["gaveUp"] == 1
    assert graph.status_of("stranded") == print_policy.FAILED
    assert graph.field("stranded", "Print_Message"), "a reason must be recorded"
    assert graph.status_of("fresh") == print_policy.PENDING,         "a healthy in-flight job is untouched"


def test_the_stale_reporting_fields_are_gone(graph, frozen_now):
    """They described a hole that no longer exists. Leaving them would report
    zero forever and imply the cliff was still there."""
    graph.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("1825", state="processing", created=iso(minutes=1))

    payload = as_json(post(POLL, POLL_BODY))

    assert "staleCount" not in payload
    assert "staleItems" not in payload


def test_an_old_row_is_still_read_before_it_is_judged(graph, frozen_now):
    """Giving up is a decision, not a filter. The job is inspected first, so one
    that completed just before the deadline is recorded as printed rather than
    failed -- and the outstanding job of one that did not is cancelled, so
    PRINT_FAILED cannot be contradicted by paper appearing later."""
    graph.add_item("printed", status=print_policy.PENDING, job_id="1830",
                   printer=graph.SHARE_ID, created=iso(days=40))
    graph.add_item("hopeless", status=print_policy.PENDING, job_id="1831",
                   printer=graph.SHARE_ID, created=iso(days=40))
    graph.add_job("1830", state="completed", created=iso(days=40))
    graph.add_job("1831", state="stopped", created=iso(days=40))

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["completed"] == 1
    assert payload["gaveUp"] == 1
    assert graph.status_of("printed") == print_policy.COMPLETED
    assert graph.cancelled == ["1831"], "only the hopeless job is cancelled"
