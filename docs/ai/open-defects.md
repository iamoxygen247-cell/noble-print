# Open defects

Defects found and deliberately **not** fixed. One row per deferred defect with a
measured rate where one exists. When fixed, **move the row to Resolved with the test that pins it** rather than
deleting it, so a future regression on the same input is recognisable. (There is
no sha to record: the project is not under version control yet.)

| # | ID | Defect | Measured rate | Cost today | Fix shape |
|---|----|--------|---------------|------------|-----------|
| — | —  | none recorded yet | — | — | — |

## Known, accepted behaviours (not defects)

These are consequences of the requirement as written, recorded so nobody
"discovers" them later and treats them as bugs.

| ID | Behaviour | Why it is accepted |
|---|---|---|
| G1 | A file stuck at `PRINT_PENDING` past 20 days is excluded by **both** Poll and Resubmit and will never be touched again. | The 20-day window is the requirement's own. Surfaced rather than fixed: Poll reports `staleCount`/`staleItems`, and `scripts/test.py stale` lists them for manual handling. |
| UC-9 | If Universal Print purges a finished job before Poll observes it, Resubmit reprints the document after 72 h. | Poll cannot distinguish "completed and purged" from "never existed", and guessing would mark documents printed that never printed. Mitigated by keeping Poll's cadence well inside the measured retention window. |
| G2 | `Printer_Name` holds the printer **share id**, not a display name. | The requirement says to store the printer id in that column. The preflight already reads `displayName`, so switching is a one-line change if wanted. |

## Resolved

| # | ID | Defect | Fixed in |
|---|----|--------|----------|
| 1 | D1 | Resubmit created a replacement job without cancelling a `stopped` original, so both would print once the printer was fixed. | Design review, before any code shipped. `universal_print.cancel_job` + `RESUBMIT_CANCEL_THEN_RESUBMIT`; pinned by `test_a_stopped_job_is_cancelled_before_the_replacement`. |
| 2 | F1 | Poll capped at 15 jobs and took the oldest first: a requirement violation ("all files") **and** starvation -- long-running jobs held every slot, so newer completed jobs were never marked and aged past the 20-day window into limbo. | Post-implementation review. Poll now checks every job in the window, bounded only by the budget, and reports `uncheckedCount`. `test_poll_checks_more_than_fifteen_pending_jobs`, `test_a_long_running_job_does_not_starve_newer_completed_ones`. |
| 3 | F2 | Resubmit ordered its retry queue by `createdDateTime`, which never changes, so a chronically failing file was re-picked every run and blocked everything newer. | Post-implementation review. `select_least_recently_attempted` orders on `lastModifiedDateTime`; eligibility still uses `createdDateTime` per R16. `test_resubmit_does_not_re_pick_the_same_failing_files_forever`. |
| 4 | F3 | Resubmit cancelled the old job on the **new** printer when the request overrode the printer. The cancel 404'd, a 404 reads as "already gone", so it reported a clean cancel while the original job stayed alive -- the exact double-print the cancel exists to prevent. | Post-implementation review. `_share_for` resolves the ORIGINAL share from the item's `Printer_Name`. `test_cancel_targets_the_original_printer_not_the_new_one`. |
| 5 | F4 | `FakeGraph` dropped `$filter` from its `@odata.nextLink`, so page 2 came back unfiltered. A flaw in the **test harness**: real Graph carries the query forward, so no paging test could have caught a real filter-loss bug. | Post-implementation review. `test_paging_returns_only_rows_matching_the_filter`. |
| 6 | F5 | Poll stamped every completion with the request's start time. With F1's cap removed a run can span 90 s, so dozens of jobs would share one timestamp up to a minute and a half stale. | Post-implementation review. `test_each_completion_is_stamped_when_it_was_observed`. |
| 7 | F6 | Resubmit could process one file twice: its two status queries run at different instants, so a file whose status changed between them appeared in both result sets. | Post-implementation review. De-duplicated by item id. `test_a_file_appearing_in_both_status_queries_is_handled_once`. |
| 8 | S1 | The final PATCH -- the one recording `Print_JobId` -- was unguarded. A failure there aborted the whole batch with a 500 **and** left the row at `PRINT_PENDING` with an empty job id, which §5.3 defines as a crashed submission Resubmit owns. A document that printed fine would be reprinted 72 h later: the silent double print the claim-first ordering exists to prevent, reintroduced one line from the end. | Second review. The write is guarded, the batch continues, the item record carries a `warning`, and an ERROR line names the coming duplicate. `test_a_lost_job_id_write_does_not_abort_the_batch`, `test_a_lost_job_id_write_is_reported_as_a_duplicate_risk`. |
| 9 | S2 | The "whole-invocation" wall-clock budget started **after** site resolution, the preflight and the status query, so that time was free. A slow query plus a full file loop could overshoot Power Automate's ~120 s connector budget -- and a connector that has given up never receives `remainingReady`, so the flow neither loops nor notifies. | Second review. `Budget(started=started)` anchors it at the request. `test_the_budget_covers_the_whole_invocation_not_just_the_file_loop`, `test_the_budget_is_shared_by_poll_too`. |
| 10 | S3 | An offline printer returned `remainingReady: -1`. Flow A's `Do Until remainingReady = 0` can never be satisfied by -1, so it spun to its iteration cap every recurrence; and because the run is a 200 with `failed = 0`, the notify condition never fired either. An offline printer was **silent**. | Second review. `remainingReady: 0` plus `printerAvailable` on every Submit response, and Flow A's condition now tests it. `test_an_offline_printer_ends_the_loop_rather_than_spinning_it`, `test_an_offline_printer_is_visible_to_the_flow`. |
| 11 | S4 | `GRAPH_TIMEOUT_SECONDS` was in the settings template, in the deploy runbook and in design §6.5 as a tunable -- and **no code read it**. Setting it did nothing, silently. | Second review. `graph_client.resolve_timeout_seconds()`, applied to the authenticated client and both unauthenticated paths. `test_graph_timeout_seconds_is_honoured` and four range cases. |
| 12 | S5 | Poll visited `PRINT_PENDING` rows with an empty `Print_JobId` and counted them in no field: not `checked`, not `uncheckedCount`, not `malformed`. The one state §5.3 tells you to expect was the one the response could not show, and the counts did not add up to the rows in the window. | Second review. `awaitingResubmit`, with `checked + awaitingResubmit + uncheckedCount == pendingInWindow` asserted. `test_poll_accounts_for_every_pending_row_in_the_window`. |
| 13 | S6 | `live-printer-check.ps1`'s upload `PUT` used `Invoke-WebRequest` without `-UseBasicParsing`, so Windows PowerShell 5.1 stopped mid-upload with an interactive *"Script Execution Risk ... continue?"* prompt. Harmless with a human watching; an indefinite hang without one. | Found by running it, 2026-08-30. `-UseBasicParsing` added. |
| 14 | S7 | The same script wrote its generated test PDF into the repo root, leaving an untracked artifact in the working tree. | Found by running it, 2026-08-30. Writes to TEMP now; `.gitignore` also guards the old location. |
| 15 | S8 | The script printed Graph's UTC timestamps with the local short format, so `Last seen` read eight hours ahead of Vancouver and a healthy printer looked broken ("8/31/2026 5:24 AM" when it was 10:24 PM on the 30th). | Second review. `Write-Timestamp` labels the zone and shows both. |
