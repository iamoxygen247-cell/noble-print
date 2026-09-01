# End-to-end testing runbook

Three things you can test from your own PowerShell window, answering different
questions. Do them in this order — each assumes the one before it passed.

| | Question it answers | Needs |
|---|---|---|
| **Part A — the printer** | Does a PDF come out of this printer, using the app's own conversion and settings? | PowerShell + the venv |
| **Part B — the happy path** | Does the app find files in SharePoint and print them? | `func start` locally, SharePoint, the printer |
| **Part C — the retry lifecycle** | Does it *recover* when a print does **not** come out? | the same, plus a printer you can switch off |

**Part A needs no Azure resources at all.** Run it first: if the printer cannot
render what the app produces, nothing later can work.

**Parts B and C run entirely against the local host** — `http://localhost:7071`,
no deployment required. The deployed variant is the same commands plus two flags
(B7).

> **Read the paper, not the exit code.** Universal Print reports `completed` for
> a page that came out cropped, scaled or blank. Every "did it work?" in this
> document means *look at the sheet*.

> **The endpoint shape changed on 2026-09-01.** There are now **two** endpoints,
> not three: `/api/print/resubmit` is gone and Poll absorbed its recovery work.
> Part C is entirely new, and none of it can be tested by the old commands.

---

# Part A — Printer end to end, no Azure

## What it proves

`scripts\live-print-test.ps1` runs the same decisions the Function App makes:
it reads the printer's live capabilities, asks `functionapp/printing` which
profile applies, converts at the resolution that profile chose, and submits with
that profile's job configuration.

It deliberately carries **no print settings of its own**. That matters because
the raster and the job configuration are a matched pair — the converter renders
full-bleed at the media size and depends on `scaling: fit` plus the device
margins to place it on the sheet. A bench script with hardcoded settings can
print a perfect page while the deployed app prints cropped. A green run here is
evidence about the code that ships.

## Prerequisites

```powershell
cd "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"

# Should print a version. If not: Install-Module Microsoft.Graph.Authentication -Scope CurrentUser
(Get-Module -ListAvailable Microsoft.Graph.Authentication | Select-Object -First 1).Version

# Should print 5.13.0 or similar
.\.venv\Scripts\python.exe -m pip show pypdfium2 | Select-String '^Version'
```

Paper in the tray, printer awake.

## A1. Offline suite first

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Expect **389 passed** in about half a second. Red here means stop — do not spend
paper diagnosing something the suite already knows about.

## A2. Is the printer reachable and still the printer we think?

```powershell
.\scripts\live-printer-check.ps1 -DiagnoseOnly
```

Sign in with the device code when prompted. Confirm:

- `Is accepting jobs : True`
- **Content types** lists `image/pwg-raster`

If the share id fails to resolve, the share was re-created and a **new share id**
was minted. Read the current one from *Universal Print → Printers → the printer →
Overview*; the script lists the ids that do exist.

## A3. Dry run — converts, prints nothing

```powershell
.\scripts\live-print-test.ps1 -PdfPath ".\samples\invoice.pdf" -PlanOnly
```

| Line | Expect |
|---|---|
| `Profile` | `pdf-to-pwg-raster` |
| `Upload as` | `image/pwg-raster` |
| `Raster dpi` | `300` |
| `PWG signature` | `RaS2` |
| `Size` | ~1,423,827 bytes for `samples\invoice.pdf` |

It also prints the **full job configuration** the app would send. `scaling: fit`
and the four `margin` values are the load-bearing pair; `dpi` here must equal the
`Raster dpi` above, or the page prints scaled.

## A4. Print it

```powershell
.\scripts\live-print-test.ps1 -PdfPath ".\samples\invoice.pdf"
```

Watch for `Job id`, `Upload : HTTP 201`, then polling to `state=completed` —
usually well under a minute.

## A5. Inspect the sheet

This is the test.

- The whole invoice is on the page, nothing clipped at any edge
- Text is sharp, not soft or doubled (soft suggests a resolution mismatch)
- Orientation and scale are right

## Part A troubleshooting

| Symptom | Cause and fix |
|---|---|
| `The share id did not resolve` | The share was re-created. Get the new id (A2) and update `-ShareId`, the script default, README and the Power Automate flows |
| `Missing Microsoft Graph scopes` | `Disconnect-MgGraph`, re-run, accept all four |
| `Conversion failed` | Damaged or password-protected PDF. Try another file |
| Job stays `pending` past the timeout | Universal Print accepted it but nothing is delivering it — printer asleep, offline, or the registration is broken |
| Job ends `aborted` | The device rejected the raster. Re-run with `-KeepRaster` and inspect the `.pwg` |
| **Page prints cropped or scaled** | The raster and job configuration disagree. This is the failure this test exists to catch — Graph will still say `completed` |

Other flags: `-KeepRaster` keeps the `.pwg`, `-ShareId` targets another printer,
`-PollSeconds` extends the wait, `-PdfPath` takes any PDF.

---

# Part B — The app end to end, from your own PowerShell window

The app has **two endpoints**, both HTTP `POST`:

| | Route | What it does |
|---|---|---|
| **Submit** | `/api/print/submit` | Claims the oldest `PRINT_READY` files and creates print jobs |
| **Poll** | `/api/print/status` | Checks `PRINT_PENDING` jobs: marks the finished, **requeues the stalled**, **fails the hopeless** |

> There is **no** `/api/print/resubmit`. It was retired on 2026-09-01 and its
> recovery work folded into Poll. If you have an older copy of these notes, any
> `test.py resubmit` or `test.py stale` command in it will now fail with an
> argument error — that is the harness telling you the endpoint is gone.

`scripts\test.py` is the **same harness** for the local host and the deployed
app. Everything below runs against the local host, which is where you are now;
the deployed variant is one flag, in B7.

## B0. Start the host

Two windows. In the first:

```powershell
cd "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"
.\scripts\start-local.ps1          # Ctrl+C stops it
```

Leave it running and watch it — the `RUN_SUMMARY` and `PRINT_EVENT` lines scroll
past there, and they are the only place you can see *why* Poll did what it did.

Everything else goes in a **second** window, also at the repo root. Set these
once so the commands below stay short:

```powershell
$PY  = ".\.venv\Scripts\python.exe"
$LIB = "AI_DropBox_V2026"
$FLD = "/Backup/Invoice"
$SHARE = "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5"
```

**Prerequisites**, all one-time:

```powershell
# 1. local.settings.json exists and is filled in
Copy-Item functionapp\local.settings.json.template functionapp\local.settings.json
#    then set GRAPH_TENANT_ID, GRAPH_CLIENT_ID, SHAREPOINT_HOSTNAME,
#    SHAREPOINT_SITE_PATH -- see docs/deploy-to-azure.md steps 1 and 6

# 2. a refresh token. Locally you may use PRINT_REFRESH_TOKEN instead of a vault:
$PY scripts\bootstrap_token.py --print-only
#    paste the value into local.settings.json as PRINT_REFRESH_TOKEN
#    (it logs a warning on every use -- that is deliberate, and it must NEVER
#     be set in Azure)

# 3. TLS, if Graph calls fail with CERTIFICATE_VERIFY_FAILED
$PY -m pip install truststore
```

## B1. The validation path — must 400, must touch nothing

```powershell
$PY scripts\test.py badpayload
```

Expect `HTTP 400 (expected 400)` and `OK: rejected without touching the queue.`
A malformed request must be refused **before** anything is claimed, or a bad call
would lock files out until its own retry.

## B2. Dry run — resolves everything, prints nothing

```powershell
$PY scripts\test.py dryrun --library $LIB --folder $FLD --printer-share-id $SHARE
```

The highest-value call here. Expected shape:

```
HTTP 200
library      : AI_DropBox_V2026
folder       : /Backup/Invoice
columns      :
    Print_Status   -> Print_x005f_Status  (encoded)
    ...
printer      : Brother MFC-L5800DW series (4429bf4e-...)
  printer id : cf8d9fa1-...   <- cancel uses this, not the share id
  accepting  : True
  content    : image/pwg-raster
  dpis       : 300, 600
conversion   : pdf-to-pwg-raster  (application/pdf -> image/pwg-raster)
  job config : colorMode=grayscale, copies=1, dpi=300, ...
candidates   : 2
    would submit 7 (invoice.pdf)
```

Check four things:

1. **`columns`** — the real internal names. `Print_x005f_Status` means SharePoint
   encoded them; either form is fine, the app resolves at runtime.
2. **`conversion`** — must be `pdf-to-pwg-raster`. `NONE` means every file would
   fail at preflight.
3. **`job config`** — should match what Part A printed with.
4. **`candidates`** — files actually found with `PRINT_READY`.

## B3. One real file

```powershell
$PY scripts\test.py submit --library $LIB --folder $FLD `
    --printer-share-id $SHARE --batch-size 1
```

`--batch-size 1` is not caution for its own sake: if the configuration is wrong
you have wasted one sheet rather than five.

Then **open the SharePoint library** and read the four columns back. This is the
step people skip, and the one that catches a silent write failure:

| Column | Expect |
|---|---|
| `Print_Status` | `PRINT_PENDING` |
| `Printer_Name` | the **share** id you passed |
| `Print_JobId` | a short number — job ids start at 1 per printer, not a GUID |
| `Print_Message` | whatever it held before — **the claim no longer clears it** |

## B4. Mark it complete once the page is out

```powershell
$PY scripts\test.py status --library $LIB --folder $FLD
```

New output shape. The `settings` line is new, and so are `requeued`, `gaveUp` and
`pendingFound`; `awaitingResubmit`, `staleCount` and `staleItems` are gone:

```
HTTP 200
settings        : stall=5min giveUp=10d maxRetries=10
failed          : 0
checked         : 1
completed       : 1
requeued        : 0
gaveUp          : 0
stillRunning    : 0
notFound        : 0
malformed       : 0
pendingFound    : 1
uncheckedCount  : 0
budgetExhausted : False
```

`checked + uncheckedCount` must always equal `pendingFound`. If it does not, a row
was examined and counted nowhere — that was defect S5, and the invariant is the
only thing that would show it.

Then read the columns again: `Print_Status` = `PRINT_COMPLETED` and
`Print_Message` = `printed on …`. `Print_JobId` must survive — it is the audit
trail, and a completion **replaces** the message, so any retry history from
earlier attempts is correctly gone.

## B5. Prove the settings are read from the request

This is the point of the change, so test it explicitly:

```powershell
# out of range -> 400, and nothing is written
$PY scripts\test.py status --library $LIB --folder $FLD --stall-minutes 0
$PY scripts\test.py status --library $LIB --folder $FLD --give-up-days 400

# in range -> echoed back in the `settings` line
$PY scripts\test.py status --library $LIB --folder $FLD `
    --stall-minutes 1 --give-up-days 2 --max-retries 3
```

The `settings` line must show what you sent, not the defaults. That line is how
you confirm a Power Automate flow is actually sending what you think it is.

## B6. Other flags

```powershell
$PY scripts\test.py status --library $LIB --folder $FLD --json   # raw response
```

## B7. Against the deployed app instead

Identical commands plus two flags. Nothing else changes:

```powershell
$HOST_NAME = az functionapp show --resource-group rg-noble-print `
    --name func-noble-print --query "defaultHostName" -o tsv
$BASE = "https://$HOST_NAME"
$KEY  = az functionapp keys list --resource-group rg-noble-print `
    --name func-noble-print --query "functionKeys.default" -o tsv

$PY scripts\test.py dryrun --base-url $BASE --key $KEY `
    --library $LIB --folder $FLD --printer-share-id $SHARE
```

Flex Consumption hashes the default hostname, so read `defaultHostName` rather
than constructing it. Never paste a function key into a file that gets committed.

> **`az` and the corporate TLS proxy.** Plain `az` fails with
> `CERTIFICATE_VERIFY_FAILED` unless it goes through the truststore wrapper. In an
> interactive PowerShell window the profile function handles it. Anywhere else,
> call it by absolute path — see `docs/ai/troubleshooting.md`.

A local run cannot prove managed identity, RBAC, application settings, packaging
or cold start. See `README.md`, "What a local run cannot prove".

## Part B troubleshooting

| Symptom | Cause and fix |
|---|---|
| `invalid choice: 'resubmit'` / `'stale'` | Both are gone. Recovery is Poll's now — see Part C |
| `could not reach http://localhost:7071` | `start-local.ps1` is not running, or it failed to start. Check the first window |
| `HTTP 401` (deployed only) | Missing or wrong `--key` |
| `remedy: run scripts/bootstrap_token.py` | The delegated refresh token is dead. A password change or reset revokes it; expiry alone does not |
| `remedy: add the missing column(s)` | The four columns are missing or renamed |
| Every query fails | `Print_Status` is not indexed. A non-indexed column cannot be used in a Graph `$filter` at all |
| `printerAvailable: false` | The printer is not accepting jobs. HTTP 200 with `failed=0`, so only that flag reveals it |
| `budgetExhausted: true` | The 90 s budget ran out mid-batch. The remainder is picked up next run — expected under load, not an error |
| `!!` warning on an item | The document printed but its job id could not be recorded, so Poll reads the row as a crashed submission and **requeues it within roughly ten minutes**. Fix `Print_JobId` by hand, fast, or set the row to `PRINT_COMPLETED` |

---

# Part C — The retry lifecycle

**This is the behaviour that is new**, and none of it existed before Resubmit was
retired. Part B proves a document can print; Part C proves the pipeline recovers
when one does not.

## The one thing that will confuse you

The retry schedule is measured from the **file's `Created` time in SharePoint**,
and how many retries are already "spent" is read off **when the current attempt
started** — the print job's own `createdDateTime`, or the row's `Modified` stamp
when there is no job.

That has a consequence worth knowing before you start:

> **A row you have just hand-edited will not requeue.** Editing it sets `Modified`
> to now, which reads as "this attempt started now", so no new boundary has been
> crossed since. Verified: at *every* file age and *every* stall setting, a row
> edited this second stays put.

So: edit the row, then **wait for the next boundary**, then run `status`. On a
fresh file with `--stall-minutes 1` the boundaries are 1, 3, 7, 15, 31 minutes
from the file's creation, so the wait is a minute or two. On a day-old file the
next boundary can be eighteen hours away — which is why every test below starts
with a **freshly uploaded** PDF.

## C1. A stalled job is cancelled and requeued

The cleanest test, and the one that pins the double-print guard. No hand editing.

1. **Turn the printer off**, or take it offline at the panel.
2. Upload a fresh PDF to the folder and set `Print_Status` = `PRINT_READY`.
3. Submit it:

```powershell
$PY scripts\test.py submit --library $LIB --folder $FLD `
    --printer-share-id $SHARE --batch-size 1
```

Note the `Print_JobId` SharePoint now shows. The job is queued and going nowhere.

4. Wait about two minutes, then poll with a one-minute stall threshold:

```powershell
$PY scripts\test.py status --library $LIB --folder $FLD --stall-minutes 1
```

Expect `requeued : 1`, and in SharePoint:

| Column | Expect |
|---|---|
| `Print_Status` | back to **`PRINT_READY`** |
| `Print_JobId` | **empty** — the job it named was cancelled |
| `Print_Message` | `Job Id <the id from step 3> cancelled. Retry job (n)` |
| `Printer_Name` | unchanged |

In the host window you should see **two** `PRINT_EVENT` lines, in this order:

```
PRINT_EVENT ep=poll ... result=cancelled ...
PRINT_EVENT ep=poll ... result=requeued  ...
```

**The `cancelled` line is the assertion that matters.** Without it the original
job is still alive, and when you switch the printer back on it prints *alongside*
its replacement. That is defect D1, and this is the only place you can watch the
guard work.

The `(n)` is the retry NUMBER the schedule is up to, derived from the file's age
— not a count of your attempts. With `--stall-minutes 1` on a three-minute-old
file it will read `(2)`, because two boundaries (1 min and 3 min) have passed. Do
not expect it to start at 1.

5. Repeat submit → wait → status two more times. Each cycle **appends** to
   `Print_Message`, and the entries must accumulate:

```
Job Id 12 cancelled. Retry job (2) | Job Id 13 cancelled. Retry job (3)
```

If the second cycle *replaces* rather than appends, the claim has started
clearing the column again — that regression is pinned by
`test_the_retry_history_survives_the_next_submit`.

6. **Turn the printer back on** and submit once more. Exactly **one** sheet must
   come out. More than one means a cancel silently failed — check the host window
   for `could not cancel print job`.

## C2. A crashed submission is recovered

The `PRINT_PENDING` row with an empty `Print_JobId` — normal, not an error. It is
what a crash between the claim and the job creation leaves behind, and Poll now
owns it.

1. Upload a fresh PDF. **Immediately** set, by hand in SharePoint:
   `Print_Status` = `PRINT_PENDING`, `Printer_Name` = the share id,
   `Print_JobId` = empty.
2. **Wait two minutes** — see the warning above; the row will not move before the
   next boundary.
3. `status --stall-minutes 1`

Expect `requeued : 1` and the row back at `PRINT_READY`. `graph.cancelled` stays
empty in this case — there is no job to cancel, and the host window shows a
`requeued` event with no `cancelled` before it.

## C3. Giving up

Needs a file **older than one day**, because `giveUpDays` has a floor of 1. Use
any old PDF already in the library rather than waiting.

1. Set an old file to `Print_Status` = `PRINT_PENDING` with a `Printer_Name`.
2. `status --give-up-days 1`

This one fires **immediately** — the give-up test runs before the stall test, so
it does not care whether the job is stalled or how long ago you edited the row.

| Column | Expect |
|---|---|
| `Print_Status` | **`PRINT_FAILED`** |
| `Print_Message` | `gave up after 1 day(s) and N retries; outstanding job cancelled` |
| `Print_JobId` | **kept** — the audit trail on a failed row |

`PRINT_FAILED` is **terminal**. Nothing retries it; the row waits for a human.
That is the deliberate trade for having deleted Resubmit, and it is why the
message has to be worth reading.

## C4. What you cannot test in an afternoon

| Behaviour | Why | Nearest thing you can do |
|---|---|---|
| The full ten-retry backoff | The last retry falls due at **3d 13h** | Trust `test_each_retry_falls_due_at_its_boundary`, which pins all ten literally |
| The grace period | Runs from 3d 13h to 10 days | `status --max-retries 1` on a stalled file: expect `requeued : 0`, `gaveUp : 0`, status still `PRINT_PENDING`, and **no** cancel — the last job is deliberately left alive |
| Retry #2 | Poll's 10-minute cadence swallows the 5- and 15-minute boundaries | Nothing to fix. Nine requeues fire, not ten; the numbering skips 2 |

## Part C troubleshooting

| Symptom | Cause and fix |
|---|---|
| `requeued : 0` when you expected 1 | Almost always the timing above — you edited the row too recently, or the next boundary has not arrived. Check the file's `Created` time, not `Modified` |
| `requeued : 0`, `stillRunning : 1` | The job is not stalled yet. Its own `createdDateTime` must be older than `stallMinutes` — a job created 30 seconds ago is not stalled at the 5-minute default |
| Requeued but no `cancelled` event | Either there was no job (C2, expected), or the cancel failed — look for `could not cancel print job` in the host window. **A duplicate print is possible** |
| Two sheets after the printer comes back | A cancel failed silently on an earlier cycle. This is the failure C1 exists to catch |
| `Print_Message` replaced instead of appended | The claim is clearing the column again. Run `pytest -k retry_history` |
| Give-up did nothing | The file is younger than `giveUpDays`. The floor is 1 day; you cannot test this on a file uploaded today |
