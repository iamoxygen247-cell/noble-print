"""
test_dryrun_and_stale.py — the two operator-facing affordances.

Both exist because of specific failure modes that are otherwise invisible:

* dryRun answers "will this work here?" before a single sheet of paper is
  committed. It resolves the same site, library, columns and printer the real
  run uses, so a wrong column name or an offline printer surfaces in the
  runbook step rather than in the tray.

* staleCount surfaces the requirement's own 20-day cliff (G1). A file past the
  window is excluded by BOTH Poll and Resubmit, so nothing will ever touch it
  again -- and without this, nothing would ever say so.
"""

from __future__ import annotations

import print_policy
from helpers import POLL, SUBMIT, as_json, iso, post

SUBMIT_BODY = {"library": "Documents", "folder": "/Invoices/ToPrint",
               "printerShareId": "share-guid"}
POLL_BODY = {"library": "Documents", "folder": "/Invoices/ToPrint"}


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


# --- stale reporting ----------------------------------------------------------


def test_poll_reports_files_stranded_past_the_window(graph, frozen_now):
    """G1/UC-10: past 20 days a PRINT_PENDING file is invisible to both Poll and
    Resubmit. It will never print and never be marked failed."""
    graph.add_item("fresh", status=print_policy.PENDING, job_id="1825",
                   printer=graph.SHARE_ID, created=iso(days=2))
    graph.add_item("stranded", status=print_policy.PENDING, job_id="1826",
                   printer=graph.SHARE_ID, created=iso(days=40))
    graph.add_job("1825", state="processing")

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["staleCount"] == 1
    assert [f["itemId"] for f in payload["staleItems"]] == ["stranded"]


def test_stale_files_are_reported_but_never_written_to(graph, frozen_now):
    """Reporting must not become acting: deciding what to do with a stranded
    file is a human's call, not this endpoint's."""
    graph.add_item("stranded", status=print_policy.PENDING, job_id="1826",
                   printer=graph.SHARE_ID, created=iso(days=40))
    graph.add_job("1826", state="completed")

    post(POLL, POLL_BODY)

    assert graph.status_of("stranded") == print_policy.PENDING
    assert not graph.calls_to("/items/stranded/fields", method="PATCH")


def test_no_stale_files_reports_zero(graph, frozen_now):
    graph.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("1825", state="processing")

    assert as_json(post(POLL, POLL_BODY))["staleCount"] == 0
