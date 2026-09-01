"""
test_review_findings.py — regressions found during the post-implementation review.

Each test here failed when it was written. They are kept separate from the
endpoint suites so the findings stay legible as a set; the behaviours they pin
belong to Poll.

  F1  Poll silently ignored pending jobs beyond the first 15, and always took
      the OLDEST 15 -- so long-running jobs permanently starved newer ones.
      Also a straight requirement violation: "query ... for ALL files".
  F2  Resubmit ordered by createdDateTime, which never changes, so chronically
      failing files were re-picked every run and blocked everything newer.
  F3  Resubmit cancelled the old job on the WRONG printer when the request
      overrode the printer, got a 404, and counted that as a successful cancel --
      leaving the original job alive to print alongside its replacement.
  F6  Resubmit's two status queries ran at different instants, so a file whose
      status changed between them was processed twice in one run.

F2, F3 and F6 were all properties of Resubmit's SHAPE, and Resubmit is gone. Each
is now structurally impossible rather than merely fixed; the sections below say
why, and name the Poll tests that carry whatever part of the guard still applies.
Their original assertions could not survive the endpoint they exercised, but
deleting a regression test without recording the reason is how a fixed defect
comes back.
"""

from __future__ import annotations

import print_policy
from helpers import POLL, SUBMIT as SUBMIT_ROUTE, as_json, iso, post

POLL_BODY = {"library": "Documents", "folder": "/Invoices/ToPrint"}


# --- F1: Poll must check every pending job in the window ----------------------


def test_poll_checks_more_than_fifteen_pending_jobs(graph, frozen_now):
    """The requirement says "querying the sharepoint folder for ALL files within
    the last 20 days with PRINT_STATUS = PRINT_PENDING".

    Capping at a batch size silently leaves the rest unchecked, and there is no
    loop in the Poll flow to pick them up.
    """
    for i in range(40):
        job_id = "job-{}".format(i)
        graph.add_item(str(i), status=print_policy.PENDING, job_id=job_id,
                       printer=graph.SHARE_ID, created=iso(days=1))
        graph.add_job(job_id, state="completed")

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["completed"] == 40, (
        "Poll checked only {} of 40 pending jobs".format(payload["completed"]))
    for i in range(40):
        assert graph.status_of(str(i)) == print_policy.COMPLETED


def test_a_long_running_job_does_not_starve_newer_completed_ones(graph, frozen_now):
    """The starvation this caused in practice.

    Fifteen old jobs stuck in `processing` would occupy every slot, run after
    run, so a newer job that HAD completed would never be marked -- and would
    eventually fall past the 20-day window and be stranded forever (G1).
    """
    for i in range(15):
        job_id = "stuck-{}".format(i)
        graph.add_item("old-{}".format(i), status=print_policy.PENDING,
                       job_id=job_id, printer=graph.SHARE_ID, created=iso(days=10))
        graph.add_job(job_id, state="processing")

    graph.add_item("newer", status=print_policy.PENDING, job_id="done",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("done", state="completed")

    payload = as_json(post(POLL, POLL_BODY))

    assert graph.status_of("newer") == print_policy.COMPLETED, (
        "the completed job was starved by older still-running ones")
    assert payload["stillRunning"] == 15


def test_poll_reports_anything_the_budget_left_unchecked(graph, frozen_now,
                                                         monkeypatch):
    """Unbounded work still needs a wall-clock guard, and when it trips the
    caller must be told rather than shown a total that looks complete."""
    import function_app

    for i in range(5):
        job_id = "job-{}".format(i)
        graph.add_item(str(i), status=print_policy.PENDING, job_id=job_id,
                       printer=graph.SHARE_ID, created=iso(days=1))
        graph.add_job(job_id, state="completed")

    calls = {"n": 0}

    def fake_exhausted(self):
        calls["n"] += 1
        return calls["n"] > 2

    monkeypatch.setattr(function_app.Budget, "exhausted", property(fake_exhausted))

    payload = as_json(post(POLL, POLL_BODY))

    assert payload["budgetExhausted"] is True
    assert payload["uncheckedCount"] > 0


def test_poll_skips_job_less_rows_without_consuming_the_run(graph, frozen_now):
    """A crashed submission -- PRINT_PENDING with no job id -- is cheap to handle
    (no Graph call for a job that does not exist). It must not displace a row that
    DOES have a job to check. Poll now requeues these rather than skipping them,
    but the ordering property this pins is unchanged."""
    for i in range(20):
        graph.add_item("nojob-{}".format(i), status=print_policy.PENDING,
                       job_id="", printer=graph.SHARE_ID, created=iso(days=10))

    graph.add_item("real", status=print_policy.PENDING, job_id="done",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("done", state="completed")

    post(POLL, POLL_BODY)

    assert graph.status_of("real") == print_policy.COMPLETED


# --- F2 and F3: retired with the Resubmit endpoint ---------------------------
#
# Both defects were properties of Resubmit's shape, and both are now structurally
# impossible rather than merely fixed. Recorded here because deleting a regression
# test needs a reason, not a shrug.
#
# F2 -- "Resubmit re-picked the same failing files forever". It took the oldest N
# per run, so five chronic failures filled every slot and nothing newer was ever
# reached; the fix was to order the retry queue by lastModifiedDateTime.
# POLL HAS NO BATCH CAP. It visits every pending row each run
# (`select_oldest(pending, len(pending))`), bounded only by the wall-clock budget
# and reporting whatever it could not reach as `uncheckedCount`. With no slots to
# compete for there is no starvation to order around, which is why
# `select_least_recently_attempted` went too.
#
# F3 -- "cancel targeted the new printer, not the one the job lives on". Resubmit
# accepted a `printerShareId` that could override the file's own Printer_Name, so
# the cancel 404'd on the wrong printer, read as "already gone", and the original
# stayed alive to print beside its replacement. POLL ACCEPTS NO PRINTER. It always
# resolves the row's own Printer_Name, so the two can never diverge. The surviving
# half of this guard -- that the cancel uses the PRINTER id behind that share, not
# the share id -- lives in
# test_poll.py::test_the_cancel_uses_the_printer_id_not_the_share_id, and the
# multi-printer case in
# test_poll.py::test_the_cancel_uses_the_printer_named_on_the_row.


# --- F4: paging must not lose the filter -------------------------------------


def test_paging_returns_only_rows_matching_the_filter(graph, frozen_now):
    """Graph carries the whole query into @odata.nextLink. The fake used to drop
    it, so page 2 came back unfiltered -- which meant no paging test could ever
    have caught a real filter-loss bug in the adapter.
    """
    graph.page_size = 2
    for i in range(5):
        graph.add_item("ready-{}".format(i), status=print_policy.READY,
                       created=iso(days=10 - i))
    for i in range(4):
        graph.add_item("other-{}".format(i), status=print_policy.COMPLETED)

    payload = as_json(post(SUBMIT_ROUTE, {"library": "Documents",
                                          "folder": "/Invoices/ToPrint",
                                          "printerShareId": graph.SHARE_ID,
                                          "dryRun": True}))

    assert payload["candidatesFound"] == 5, (
        "paging leaked rows the filter excluded: {}".format(payload["candidatesFound"]))


# --- F5: each completion carries its own observation time --------------------


def test_each_completion_is_stamped_when_it_was_observed(graph, monkeypatch):
    """One `now` for the whole request would stamp forty jobs with a single time
    that could be a minute and a half stale by the end of the batch."""
    import itertools
    from datetime import timedelta

    import print_policy as policy
    from helpers import NOW

    ticks = itertools.count()
    monkeypatch.setattr(policy, "now_utc",
                        lambda: NOW + timedelta(seconds=next(ticks)))

    for i in range(3):
        graph.add_item(str(i), status=policy.PENDING, job_id="j{}".format(i),
                       printer=graph.SHARE_ID, created=iso(days=1))
        graph.add_job("j{}".format(i), state="completed")

    post(POLL, POLL_BODY)

    stamps = {graph.field(str(i), "Print_Message") for i in range(3)}
    assert len(stamps) == 3, "all three completions share one timestamp: {}".format(stamps)


# --- F6: retired with the Resubmit endpoint ----------------------------------
#
# "A file appeared in both status queries and was processed twice." Resubmit ran
# TWO equality queries -- one for PRINT_PENDING, one for PRINT_FAILED -- at
# different instants, so a row whose status changed in between landed in both
# result sets and was submitted twice in one run. The fix was to de-duplicate by
# item id.
#
# POLL RUNS ONE QUERY. There is no second result set for a row to appear in, so
# the overlap cannot occur. What replaced it as the concurrency guard is the
# eTag-conditioned requeue -- two OVERLAPPING RUNS racing on one row -- pinned by
# test_poll.py::test_a_lost_etag_skips_the_requeue.


# --- remainingReady must never end the flow's loop early ---------------------


def test_a_skipped_file_still_counts_as_remaining(graph, frozen_now):
    """A file we failed to claim is either another run's now, or still READY
    because its eTag moved for an unrelated reason. Counting it as done would end
    the Do-Until with work outstanding; counting it as remaining costs at most one
    extra harmless call."""
    graph.add_item("1", status=print_policy.READY, created=iso(days=1))

    real_patch = graph._patch_fields
    state = {"bumped": False}

    def steal_then_patch(rel, query, body, headers):
        if not state["bumped"] and "/items/1/fields" in rel:
            state["bumped"] = True
            graph.items["1"]["eTag"] = '"1,99"'
        return real_patch(rel, query, body, headers)

    graph._patch_fields = steal_then_patch

    payload = as_json(post(SUBMIT_ROUTE, {"library": "Documents",
                                          "folder": "/Invoices/ToPrint",
                                          "printerShareId": graph.SHARE_ID}))

    assert payload["skipped"] == 1
    assert payload["remainingReady"] >= 1, "a skipped file was counted as done"
