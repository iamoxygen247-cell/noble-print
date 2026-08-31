"""
test_second_review.py — regressions found in the second review pass.

Each test here failed when it was written. They are kept apart from
test_review_findings.py so the two review passes stay separately legible.

  S1  A failure on the FINAL patch -- the one that records Print_JobId -- was
      unguarded. It aborted the whole batch with a 500 AND left the file at
      PRINT_PENDING with an empty job id, which the design defines as "a crashed
      submission Resubmit owns". So a document that printed perfectly well would
      be reprinted 72 hours later: the exact silent double print the claim-first
      ordering exists to prevent, reintroduced one line from the end.
  S2  The "whole-invocation" wall-clock budget was started AFTER site resolution,
      the printer preflight and the SharePoint query. Time spent there did not
      count, so a slow query plus a full file loop could run well past Power
      Automate's ~120 s connector budget -- and a connector that gives up never
      receives remainingReady, so the flow neither loops nor notifies.
  S3  An offline printer returned remainingReady = -1. Flow A's documented
      "Do Until remainingReady = 0" is never satisfied by -1, so it spun to its
      iteration cap every cycle, and because the run is a 200 with failed = 0 the
      notify condition never fired either. An offline printer was silent.
  S4  GRAPH_TIMEOUT_SECONDS was in the settings template, in the deploy runbook
      and in design §6.5 as a tunable -- and no code read it. Setting it did
      nothing, silently.
  S5  Poll visited PRINT_PENDING rows with an empty Print_JobId and counted them
      nowhere: not in `checked`, not in `uncheckedCount`, not in `malformed`. A
      normal, expected state (design §5.3) was invisible in the response, and the
      counts did not add up to the number of rows in the window.
"""

from __future__ import annotations

import logging

import pytest

import function_app
import graph_client
import print_policy
import sharepoint
from fake_graph import FakeGraph
from helpers import POLL, SUBMIT, as_json, iso, post

BODY = {"library": "Documents", "folder": "/Invoices/ToPrint"}


def submit_body(**overrides):
    """Submit's body. The share id comes from FakeGraph rather than a literal, so
    the two cannot drift apart silently."""
    return dict(BODY, printerShareId=FakeGraph.SHARE_ID, **overrides)


# --- S1: losing the job-id write must not cause a reprint ---------------------


def _break_the_job_id_write(monkeypatch):
    """Fail only the PATCH that writes Print_JobId, leaving every other write
    working -- so the claim succeeds, the job is created and started, and only
    the last step falls over."""
    real = sharepoint.patch_fields

    def flaky(client, context, item_id, values, etag=None):
        if list(values) == [print_policy.COLUMN_JOB_ID]:
            raise RuntimeError("SharePoint rejected the job-id write")
        return real(client, context, item_id, values, etag=etag)

    monkeypatch.setattr(function_app.sharepoint, "patch_fields", flaky)


def test_a_lost_job_id_write_does_not_abort_the_batch(graph, monkeypatch):
    """One failed write must not discard the outcome of every other file.

    The response is the only way Power Automate learns what happened. A 500 here
    threw away the record of files that had already printed."""
    _break_the_job_id_write(monkeypatch)
    graph.add_item("1", status=print_policy.READY, created=iso(days=2))
    graph.add_item("2", status=print_policy.READY, created=iso(days=1))

    response = post(SUBMIT, submit_body())

    assert response.status_code == 200, "one bad write aborted the whole run"
    payload = as_json(response)
    assert len(payload["items"]) == 2, "the second file was never attempted"
    assert len(graph.created_jobs()) == 2


def test_a_lost_job_id_write_is_reported_as_a_duplicate_risk(graph, monkeypatch,
                                                             caplog):
    """The document IS printing -- the job was created and started. What is lost
    is our record of it, and the consequence is specific: Resubmit will treat the
    row as a crashed submission and print it again in 72 hours. That has to be
    loud, because nothing else will ever say it."""
    _break_the_job_id_write(monkeypatch)
    graph.add_item("1", status=print_policy.READY, created=iso(days=1))

    with caplog.at_level(logging.WARNING):
        payload = as_json(post(SUBMIT, submit_body()))

    record = payload["items"][0]
    assert record["result"] == function_app.RESULT_SUBMITTED
    assert record.get("jobId"), "the job id must survive in the response"
    assert "duplicate" in (record.get("warning") or "").lower()

    logged = " ".join(r.getMessage() for r in caplog.records
                      if r.levelno >= logging.ERROR)
    assert "duplicate" in logged.lower(), \
        "the only warning that a reprint is coming was never logged"


# --- S2: the budget must cover the whole invocation ---------------------------


def test_the_budget_covers_the_whole_invocation_not_just_the_file_loop(
        graph, monkeypatch):
    """The budget's entire purpose is to undercut Power Automate's ~120 s
    connector budget so the structured response arrives. Measuring only the file
    loop lets resolution, preflight and the query spend that time for free."""
    for n in range(3):
        graph.add_item(str(n), status=print_policy.READY, created=iso(days=n + 1))

    real_query = sharepoint.query_by_status
    burned = {"seconds": 0.0}
    real_monotonic = function_app.time.monotonic

    def slow_query(*args, **kwargs):
        burned["seconds"] += 200.0
        return real_query(*args, **kwargs)

    monkeypatch.setattr(function_app.sharepoint, "query_by_status", slow_query)
    monkeypatch.setattr(function_app.time, "monotonic",
                        lambda: real_monotonic() + burned["seconds"])

    payload = as_json(post(SUBMIT, submit_body()))

    assert payload["budgetExhausted"] is True
    assert payload["submitted"] == 0, \
        "200 s had already gone before the first file; the 90 s budget was spent"


def test_the_budget_is_shared_by_poll_too(graph, frozen_now, monkeypatch):
    graph.add_item("1", status=print_policy.PENDING, job_id="1801",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("1801", state="completed")

    real_query = sharepoint.query_by_status
    burned = {"seconds": 0.0}
    real_monotonic = function_app.time.monotonic

    def slow_query(*args, **kwargs):
        burned["seconds"] += 200.0
        return real_query(*args, **kwargs)

    monkeypatch.setattr(function_app.sharepoint, "query_by_status", slow_query)
    monkeypatch.setattr(function_app.time, "monotonic",
                        lambda: real_monotonic() + burned["seconds"])

    payload = as_json(post(POLL, BODY))

    assert payload["budgetExhausted"] is True
    assert payload["completed"] == 0
    assert payload["uncheckedCount"] == 1


# --- S3: an offline printer must not spin the flow, and must not be silent ----


def test_an_offline_printer_ends_the_loop_rather_than_spinning_it(graph):
    """Flow A loops `Do Until remainingReady = 0`. A sentinel that is never zero
    runs it to its iteration cap on every recurrence, forever."""
    graph.share["isAcceptingJobs"] = False
    graph.add_item("1", status=print_policy.READY, created=iso(days=1))

    payload = as_json(post(SUBMIT, submit_body()))

    assert payload["remainingReady"] == 0, \
        "the flow's Do-Until can never be satisfied by a negative sentinel"


def test_an_offline_printer_is_visible_to_the_flow(graph):
    """A 200 with failed = 0 matches no notify condition, so without an explicit
    flag an offline printer produces no alert of any kind."""
    graph.share["isAcceptingJobs"] = False
    graph.add_item("1", status=print_policy.READY, created=iso(days=1))

    offline = as_json(post(SUBMIT, submit_body()))
    assert offline["printerAvailable"] is False

    graph.share["isAcceptingJobs"] = True
    healthy = as_json(post(SUBMIT, submit_body()))
    assert healthy["printerAvailable"] is True, \
        "the flag must be present on every response, not only the failing one"


# --- S4: GRAPH_TIMEOUT_SECONDS must actually do something ---------------------


def test_graph_timeout_seconds_is_honoured(monkeypatch):
    monkeypatch.setenv("GRAPH_TIMEOUT_SECONDS", "45")
    client = graph_client.GraphClient(lambda: "token")
    assert client._timeout == 45.0


def test_graph_timeout_falls_back_to_the_default_when_unset(monkeypatch):
    monkeypatch.delenv("GRAPH_TIMEOUT_SECONDS", raising=False)
    client = graph_client.GraphClient(lambda: "token")
    assert client._timeout == graph_client.DEFAULT_TIMEOUT_SECONDS


@pytest.mark.parametrize("value", ["0", "-5", "9999", "not-a-number"])
def test_an_out_of_range_graph_timeout_env_warns_and_falls_back(
        monkeypatch, caplog, value):
    """A tunable's env override warns and falls back; only a REQUEST value is a
    400. A server misconfiguration must not fail every call (design §6.5)."""
    monkeypatch.setenv("GRAPH_TIMEOUT_SECONDS", value)
    with caplog.at_level(logging.WARNING):
        client = graph_client.GraphClient(lambda: "token")
    assert client._timeout == graph_client.DEFAULT_TIMEOUT_SECONDS
    assert any("GRAPH_TIMEOUT_SECONDS" in r.getMessage() for r in caplog.records)


def test_the_unauthenticated_calls_use_the_same_resolved_timeout(monkeypatch):
    """The download and the upload PUT go through the separate anonymous session.
    A timeout that applied only to authenticated calls would be a half-setting."""
    monkeypatch.setenv("GRAPH_TIMEOUT_SECONDS", "45")
    seen = {}

    class Recorder:
        def get(self, url, timeout=None):
            seen["get"] = timeout
            return type("R", (), {"status_code": 200, "content": b"x",
                                  "headers": {}, "text": "x"})()

    monkeypatch.setattr(graph_client, "_anon_session", Recorder())
    graph_client.download_unauthenticated("https://example.invalid/f")
    assert seen["get"] == 45.0


# --- S5: Poll must account for every row in the window ------------------------


def test_poll_accounts_for_every_pending_row_in_the_window(graph, frozen_now):
    """A PRINT_PENDING row with no job id is normal, not an error (design §5.3):
    Submit claimed it and crashed before creating the job, and Resubmit owns it.
    Counting it nowhere made the one state the design tells you to expect the one
    state the response cannot show."""
    graph.add_item("1", status=print_policy.PENDING, job_id="",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_item("2", status=print_policy.PENDING, job_id="1801",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("1801", state="completed")

    payload = as_json(post(POLL, BODY))

    assert payload["awaitingResubmit"] == 1
    assert (payload["checked"] + payload["awaitingResubmit"]
            + payload["uncheckedCount"]) == payload["pendingInWindow"], \
        "the counts must add up to the rows in the window"
