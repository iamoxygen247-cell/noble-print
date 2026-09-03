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

> **The endpoint shape changed on 2026-09-01.** `/api/print/resubmit` is gone —
> Poll absorbed its recovery work — and `/api/print/health` was added, so there
> are **three**: Health, Submit, Poll. Part C is entirely new, and none of it can
> be tested by the old commands.

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

Expect **543 passed** in about a second. Red here means stop — do not spend
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

The app has **three endpoints**, all HTTP `POST`:

| | Route | What it does |
|---|---|---|
| **Health** | `/api/print/health` | Can the pipeline work right now? Run it first — it writes nothing |
| **Submit** | `/api/print/submit` | Claims the oldest `PRINT_READY` files **that are due** (`Print_Time` empty or past) and creates print jobs |
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
$Repo = "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"
Set-Location -LiteralPath $Repo
& (Join-Path $Repo "scripts\start-local.ps1")   # Ctrl+C stops it
```

Leave it running and watch it — the `RUN_SUMMARY` and `PRINT_EVENT` lines scroll
past there, and they are the only place you can see *why* Poll did what it did.

Everything else goes in a **second** window. Paste this setup block first. It
uses absolute paths, so the commands continue to work even if the window was
previously in `functionapp` or another directory:

```powershell
$Repo = "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"
Set-Location -LiteralPath $Repo

$Python          = Join-Path $Repo ".venv\Scripts\python.exe"
$TestScript      = Join-Path $Repo "scripts\test.py"
$BootstrapScript = Join-Path $Repo "scripts\bootstrap_token.py"

foreach ($RequiredFile in @($Python, $TestScript, $BootstrapScript)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required file was not found: $RequiredFile"
    }
}

# The site, splatted into every call that resolves a library. It was two app
# settings (SHAREPOINT_HOSTNAME / SHAREPOINT_SITE_PATH) until 2026-09-02 and is
# request input now, so the script has to name it exactly as a flow does.
# NOT $Host -- that is a PowerShell automatic variable.
$Site = @("--hostname", "noblehomes.sharepoint.com", "--site-path", "/sites/PM")
$LIB = "AI_DropBox_V2026"
$FLD = "/Backup/Invoice"
# George MFC Printer
# $SHARE = "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5" 

#Noble Home MFC Printer
$SHARE = "5f488e73-ab80-4a6b-a60a-a0f883e17e2e"

```

> **PowerShell syntax:** the leading `&` in every Python command below is the
> call operator. It is required because `$Python` contains an executable path.
> Typing `$Python scripts\test.py ...` without `&` causes `Unexpected token`, and
> defining `$Python` as a relative path causes `not recognized` after changing
> directories.

**Prerequisites**, all one-time:

```powershell
# 1. local.settings.json exists and is filled in
if (-not (Test-Path -LiteralPath ".\functionapp\local.settings.json")) {
    Copy-Item ".\functionapp\local.settings.json.template" `
        ".\functionapp\local.settings.json"
}
#    then set GRAPH_TENANT_ID and GRAPH_CLIENT_ID -- see deploy-to-azure.md
#    step 1. The SITE is NOT a setting: pass --hostname and --site-path to
#    scripts/test.py, the same way a Power Automate flow sends them.

# 2. a refresh token. Locally you may use PRINT_REFRESH_TOKEN instead of a vault:
& $Python $BootstrapScript --print-only

#    paste the value into local.settings.json as PRINT_REFRESH_TOKEN
#    (it logs a warning on every use -- that is deliberate, and it must NEVER
#     be set in Azure)

# 3. TLS, if Graph calls fail with CERTIFICATE_VERIFY_FAILED
& $Python -m pip install truststore
```

## B0a. Health — run this before anything else

One share read, **no writes anywhere**. It is what the flows call before Submit
and Poll, so it is what you should call before the rest of Part B: if it is
unhealthy, everything below fails for a reason it already told you.

```powershell
& $Python $TestScript health --printer-share-id $SHARE --print-format image/pwg-raster
```

Expect exactly this on the real printer:

```
HTTP 200
healthy      : True
printer      : 4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5
format       : image/pwg-raster
summary      : printer <name> is ready; documents go through the pdf-to-pwg-raster profile
  accepting  : True   state: idle
  content    : image/pwg-raster
  profile    : pdf-to-pwg-raster  (converts: True)
```

### The refusal — and why it is a 200

```powershell
# application/pdf on a printer that takes raster ONLY
& $Python $TestScript health --printer-share-id $SHARE --print-format application/pdf
```

```
HTTP 200
healthy      : False
summary      : 1 problem: FORMAT_NOT_SUPPORTED
  ERROR   FORMAT_NOT_SUPPORTED: the printer does not accept application/pdf (supports: image/pwg-raster)
           -> send a printFormat the printer reports, or omit it and let the capabilities choose
```

**Still HTTP 200.** A sick printer is never a non-2xx — 400 means *your request*
was malformed and 500 means Health itself broke. Prove that distinction:

```powershell
& $Python $TestScript health --printer-share-id $SHARE --print-format image/png
#   -> HTTP 400  printFormat 'image/png' is not supported
#      no `healthy` key at all: this says nothing about the printer
```

### Switch the printer off — and **record what you see**

```powershell
# power the printer down, wait ~30s for Universal Print to notice, then:
& $Python $TestScript health --printer-share-id $SHARE --print-format image/pwg-raster
```

Expect `healthy : False` with `PRINTER_NOT_ACCEPTING_JOBS`:

```
  ERROR   PRINTER_NOT_ACCEPTING_JOBS: the printer share is not accepting jobs (state: idle)
           -> wake or reconnect the printer, then re-run this check
```

> **This step has a second job: write down the `state` line.** `PRINTER_STOPPED`
> is an error by decision, but `printerProcessingState` has **never been observed
> in this tenant as anything but `idle`** — including, possibly, right now with the
> printer off. Record the `accepting` and `state` values you actually get into the
> `status.state` row of `docs/design.md` §4. That turns the one assumption in this
> endpoint into evidence.

Compare against Submit for the same state — this is the whole argument for the
endpoint:

```powershell
& $Python $TestScript submit @Site --library $LIB --folder $FLD --printer-share-id $SHARE --batch-size 1
#   -> HTTP 200, submitted: 0, failed: 0, printerAvailable: False
#      A 200 with no failures. Health says False in one field instead.
```

Switch the printer back on before continuing.

## B1. The validation path — must 400, must touch nothing

```powershell
& $Python $TestScript badpayload
```

Expect `HTTP 400 (expected 400)` and `OK: rejected without touching the queue.`
A malformed request must be refused **before** anything is claimed, or a bad call
would lock files out until its own retry.

## B2. Dry run — resolves everything, prints nothing

```powershell
& $Python $TestScript dryrun @Site --library $LIB --folder $FLD --printer-share-id $SHARE
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
& $Python $TestScript submit @Site --library $LIB --folder $FLD `
    --printer-share-id $SHARE --batch-size 1
```

`--batch-size 1` is not caution for its own sake: if the configuration is wrong
you have wasted one sheet rather than five.

Then **open the SharePoint library** and read the five columns back. This is the
step people skip, and the one that catches a silent write failure:

| Column | Expect |
|---|---|
| `Print_Status` | `PRINT_PENDING` |
| `Printer_Name` | the **share** id you passed |
| `Print_JobId` | a short number — job ids start at 1 per printer, not a GUID |
| `Print_Message` | whatever it held before — **the claim no longer clears it** |

## B4. Mark it complete once the page is out

```powershell
& $Python $TestScript status @Site --library $LIB --folder $FLD
```

New output shape. The `settings` line is new, and so are `requeued`, `gaveUp` and
`pendingFound`; `awaitingResubmit`, `staleCount` and `staleItems` are gone:

```
HTTP 200
settings        : stall=5min giveUp=10d
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
& $Python $TestScript status @Site --library $LIB --folder $FLD --stall-minutes 0
& $Python $TestScript status @Site --library $LIB --folder $FLD --give-up-days 400

# in range -> echoed back in the `settings` line
& $Python $TestScript status @Site --library $LIB --folder $FLD `
    --stall-minutes 1 --give-up-days 2

# a flow still carrying the retired knob is accepted and ignored, not rejected --
# which is what lets the code deploy before the flows are edited
& $Python $TestScript status @Site --library $LIB --folder $FLD --json   # no maxRetries key
```

The `settings` line must show what you sent, not the defaults. That line is how
you confirm a Power Automate flow is actually sending what you think it is.

**There is no app-setting fallback to fall back to.** These used to resolve
request → app setting → default, and the app setting is now ignored entirely, so
this test is the only thing standing between a flow and silently running on the
defaults.

## B5a. Choose the upload format explicitly (`printFormat`)

Without this flag Submit picks a profile from what the **printer** reports. With
it, **you** choose, and the printer's preference does not get a vote.

> **Read this before running anything.** `$SHARE` above is the Brother
> MFC-L5800DW, and it reports **`image/pwg-raster` and nothing else** — verified
> against the live API on 2026-08-31 (README "Its capabilities and defaults").
> On *this* printer `--print-format image/pwg-raster` and omitting the flag
> select the **same** profile, because the capabilities already forced the raster
> path. So the flag cannot change the outcome here — what you are testing is that
> it is **honoured and echoed**, and that the wrong format is **refused**. On a
> printer that also accepted PDF the flag would genuinely change the pipeline.

```powershell
# 1. What WOULD be uploaded. Prints nothing.
& $Python $TestScript dryrun @Site --library $LIB --folder $FLD `
    --printer-share-id $SHARE --print-format image/pwg-raster
```

Expect exactly this, on the real printer:

```
requested fmt: image/pwg-raster
conversion   : pdf-to-pwg-raster  (application/pdf -> image/pwg-raster)
  converts   : True
```

`requested fmt` is **echoed by the endpoint**, so it is the proof the flag arrived
rather than that you typed it correctly. Run it once more with the flag omitted:
everything stays the same except

```
requested fmt: (none -- the printer's capabilities choose)
```

**If `requested fmt` still says `(none ...)` when you passed the flag, it is not
reaching the endpoint** — fix that before believing anything else here.

```powershell
# 2. Refusals. Both must 400 and claim nothing.

# 2a. a format this app cannot produce at all
& $Python $TestScript dryrun @Site --library $LIB --folder $FLD `
    --printer-share-id $SHARE --print-format image/png
#   -> 400  printFormat 'image/png' is not supported
#           (expected one of: application/pdf, image/pwg-raster)

# 2b. a supported format THIS PRINTER does not report. On the Brother that is
#     application/pdf -- it takes raster only, so asking for PDF is refused.
& $Python $TestScript dryrun @Site --library $LIB --folder $FLD `
    --printer-share-id $SHARE --print-format application/pdf
#   -> 400  printer <share display name> does not accept application/pdf
#           (supports: image/pwg-raster)
```

The message names the **share's** display name, which is whatever the `printer :`
line of a plain dry run (B2) shows — not necessarily the printer's own name.

2b is the one worth pausing on: it is a **400 at preflight**, so no row was
claimed and the queue is untouched. That is the difference between a
misconfigured flow and a damaged queue.

```powershell
# 3. For real, one file, naming the format explicitly.
& $Python $TestScript submit @Site --library $LIB --folder $FLD `
    --printer-share-id $SHARE --batch-size 1 --print-format image/pwg-raster
```

**A page comes out.** On this printer that is the same page the run without the
flag produces — the point is that naming the format did not break it.

> **The host window will not show the content type.** Nothing in the app logs it:
> `PRINT_EVENT` and `RUN_SUMMARY` carry no such field, and neither
> `universal_print` nor `printing/` logs the upload. The two places the format is
> actually visible are the **dry run's `conversion` block** (step 1) and the
> **`printFormat` echoed on the submit response** — add `--json` to see it. Do not
> go looking in the log for a line that was never written.

If the sheet comes out cropped or scaled, the job configuration and the raster
have been separated — see `printing/profiles.py`, whose module docstring explains
why `scaling: fit` is load-bearing.

## B5b. Point Poll at one printer (`printerShareId`)

Optional on `status`, and a **hard override** when sent: every job lookup and
every cancel addresses that share instead of each row's own `Printer_Name`.

```powershell
& $Python $TestScript status @Site --library $LIB --folder $FLD --printer-share-id $SHARE
```

Expect two lines:

```
printer override: 4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5
printerOverridden: 0
```

Without the flag, the `printer override:` line is absent entirely.

That zero is the one number to read. It counts rows whose `Printer_Name` disagrees
with what you passed. With one printer registered it is always 0. If it is not:

> **WARNING: N row(s) name a DIFFERENT printer from the --printer-share-id
> override.**

That is defect **F3-R** (`docs/ai/open-defects.md`), and it means those jobs cannot
be cancelled from the overriding printer and **may print twice**. Either drop the
flag or pass the share those rows actually name.

## B6. Other flags

```powershell
& $Python $TestScript status @Site --library $LIB --folder $FLD --json   # raw response
```

## B7. Against the deployed app instead

Identical commands plus two flags. Nothing else changes:

```powershell
$HOST_NAME = az functionapp show --resource-group rg-noble-print `
    --name func-noble-print --query "defaultHostName" -o tsv
$BASE = "https://$HOST_NAME"
$KEY  = az functionapp keys list --resource-group rg-noble-print `
    --name func-noble-print --query "functionKeys.default" -o tsv

& $Python $TestScript dryrun @Site --base-url $BASE --key $KEY `
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
| `Unexpected token 'scripts\test.py'` | Python was invoked through a variable without PowerShell's call operator. Re-paste the B0 setup block and run `& $Python $TestScript ...` |
| `'.\.venv\Scripts\python.exe' is not recognized` | `$Python` was defined as a relative path and the window is in `functionapp`. Re-paste the B0 setup block; it defines an absolute interpreter path and returns to the repository root |
| `invalid choice: 'resubmit'` / `'stale'` | Both are gone. Recovery is Poll's now — see Part C |
| `could not reach http://localhost:7071` | `start-local.ps1` is not running, or it failed to start. Check the first window |
| `HTTP 401` (deployed only) | Missing or wrong `--key` |
| `remedy: run scripts/bootstrap_token.py` | The delegated refresh token is dead. A password change or reset revokes it; expiry alone does not |
| `remedy: add the missing column(s)` | One of the five columns is missing or renamed — `Print_Time` is the newest, and the likeliest |
| Every query fails | `Print_Status` is not indexed. A non-indexed column cannot be used in a Graph `$filter` at all |
| `printerAvailable: false` | The printer is not accepting jobs. HTTP 200 with `failed=0`, so only that flag reveals it |
| `budgetExhausted: true` | The 90 s budget ran out mid-batch. The remainder is picked up next run — expected under load, not an error |
| `!!` warning on an item | The document printed but its job id could not be recorded, so Poll reads the row as a crashed submission and **requeues it within roughly ten minutes**. Fix `Print_JobId` by hand, fast, or set the row to `PRINT_COMPLETED` |
| `download: ... has no downloadable driveItem (is it a folder?)` on a real PDF | Defect **L1**, fixed 2026-09-01. The driveItem GET carried a `$select`, which makes Graph drop the `@microsoft.graph.downloadUrl` annotation — so **every** file failed, folder or not. If it reappears, a `$select` has come back to `sharepoint.get_download_url`; run `pytest -k select_away_the_download_url` |
| `download failed: ('Connection aborted.', ConnectionResetError(10054, ...))` | The **workstation's** TLS interception, not the pipeline — measured at two resets in three identical attempts on this machine, and absent in Azure. Retried three times with backoff since 2026-09-01; if all three are reset the row lands at `PRINT_FAILED`, so reset it to `PRINT_READY` and re-run |

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

That has a consequence worth knowing before you start, and **it is the opposite of
what this section said before 2026-09-02.**

> **A row you hand-edit to `PRINT_PENDING` with no job id requeues on the very next
> `status` run** — at every file age and every stall setting. It has no job, so it
> is stalled by definition, and Poll no longer waits for a boundary before acting.
>
> What waits is the **reprint**. The requeue writes `Print_Time`, and Submit will
> not claim the row until then. Verified against the real `poll_decision` with
> `--stall-minutes 1`: a 3-minute-old file gets a due time 4 minutes out, a
> **day-old file gets one about ten hours out**.

So the row moving straight back to `PRINT_READY` is *not* evidence the document is
about to print — read `Print_Time` before you conclude anything. This is why every
test below starts with a **freshly uploaded** PDF: on a fresh file the due times are
minutes away, on an old one they are hours.

(The old caveat here claimed such a row "will not requeue", because editing it bumps
`Modified` and no new boundary had been crossed. That reasoning applied to the
`due > spent` gate, which no longer exists — `Modified` now only influences *which*
retry number is scheduled, not *whether* one is.)

## C1. A stalled job is cancelled and requeued

The cleanest test, and the one that pins the double-print guard. No hand editing.

1. **Turn the printer off**, or take it offline at the panel.
2. Upload a fresh PDF to the folder and set `Print_Status` = `PRINT_READY`.
3. Submit it:

```powershell
& $Python $TestScript submit @Site --library $LIB --folder $FLD `
    --printer-share-id $SHARE --batch-size 1
```

Note the `Print_JobId` SharePoint now shows. The job is queued and going nowhere.

4. Wait about two minutes, then poll with a one-minute stall threshold:

```powershell
& $Python $TestScript status @Site --library $LIB --folder $FLD --stall-minutes 1
```

Expect `requeued : 1`, and in SharePoint:

| Column | Expect |
|---|---|
| `Print_Status` | back to **`PRINT_READY`** |
| `Print_JobId` | **empty** — the job it named was cancelled |
| `Print_Message` | `Job Id <the id from step 3> cancelled. Retry job (n)` |
| `Printer_Name` | unchanged |
| `Print_Time` | **a timestamp, in local time** — when the next attempt falls due. This is the new column, and the point of the whole change: the backoff is now something you read rather than infer |

**Check `Print_Time` before assuming the reprint is imminent.** The row is
`PRINT_READY` again, but Submit will decline it until that moment passes — so a
`submit` run right now can legitimately report `submitted : 0` with
`notYetDue : 1`, and that is the feature working, not a failure.

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
2. Run `status` straight away — **no waiting needed.** The row has no job, so it
   is stalled by definition and requeues on the first run. (This step used to say
   "wait two minutes"; that applied to the boundary gate that no longer exists.)
3. Run:

   ```powershell
   & $Python $TestScript status @Site --library $LIB --folder $FLD --stall-minutes 1
   ```

Expect `requeued : 1` and the row back at `PRINT_READY`, with a `Print_Time`
written. No cancel happens here — there is no job to cancel — so the host window
shows a `requeued` event with **no** `cancelled` line before it, which is the
difference from C1.

`Print_Time` will be a few minutes out on a file you just uploaded. On an older
file it can be hours: the schedule is measured from the file's creation, and a
crash does not reset it.

## C3. Giving up

Needs a file **older than one day**, because `giveUpDays` has a floor of 1. Use
any old PDF already in the library rather than waiting.

1. Set an old file to `Print_Status` = `PRINT_PENDING` with a `Printer_Name`.
2. Run:

   ```powershell
   & $Python $TestScript status @Site --library $LIB --folder $FLD --give-up-days 1
   ```

This one fires **immediately** — the give-up test runs before the stall test, so
it does not care whether the job is stalled or how long ago you edited the row.

| Column | Expect |
|---|---|
| `Print_Status` | **`PRINT_FAILED`** |
| `Print_Message` | `gave up after 1 day(s) and N retries; outstanding job cancelled` |
| `Print_JobId` | **kept** — the audit trail on a failed row |
| `Print_Time` | **emptied** — a terminal row carries no schedule, so a human resetting it to `PRINT_READY` gets an immediate print rather than a silent wait |

`PRINT_FAILED` is **terminal**. Nothing retries it; the row waits for a human.
That is the deliberate trade for having deleted Resubmit, and it is why the
message has to be worth reading.

## C4. What you cannot test in an afternoon

| Behaviour | Why | Nearest thing you can do |
|---|---|---|
| The full backoff | The last unclamped retry falls due at **7d 2h**, and the clamped one at 10 days | Trust `test_each_retry_falls_due_at_its_boundary` and `test_next_retry_time_lands_on_the_boundary`, which pin the ladder literally in both units |
| The clamp | Needs an 8-day-old file | `test_the_due_time_is_never_scheduled_past_the_give_up_deadline` and `test_the_clamped_final_attempt_is_failed_rather_than_stranded` cover both halves offline |
| The early retries | The schedule opens at retry 3 with Flow A at 15 min, because `spent` is read off the file's age when the FIRST job is created | Nothing to fix, and note it is **Flow A's** recurrence that decides this, not Flow B's — Flow B at 10 min and 1 min give identical sequences. Shortening Flow A is the only lever; lowering `stallMinutes` makes it worse |

## Part C troubleshooting

| Symptom | Cause and fix |
|---|---|
| `requeued : 0` when you expected 1 | Almost always the timing above — you edited the row too recently, or the next boundary has not arrived. Check the file's `Created` time, not `Modified` |
| `requeued : 0`, `stillRunning : 1` | The job is not stalled yet. Its own `createdDateTime` must be older than `stallMinutes` — a job created 30 seconds ago is not stalled at the 5-minute default |
| Requeued but no `cancelled` event | Either there was no job (C2, expected), or the cancel failed — look for `could not cancel print job` in the host window. **A duplicate print is possible** |
| Two sheets after the printer comes back | A cancel failed silently on an earlier cycle. This is the failure C1 exists to catch |
| `Print_Message` replaced instead of appended | The claim is clearing the column again. Run `pytest -k retry_history` |
| Give-up did nothing | The file is younger than `giveUpDays`. The floor is 1 day; you cannot test this on a file uploaded today |
