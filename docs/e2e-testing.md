# End-to-end testing runbook

Two things you can test from a local command window, and they answer different
questions. Do them in this order.

| | Question it answers | Needs |
|---|---|---|
| **Part A — printer E2E** | Does a PDF come out of this printer, using the app's own conversion and settings? | PowerShell + the venv |
| **Part B — deployed E2E** | Does the deployed Function App find files in SharePoint and print them? | A deployed app, its function key, SharePoint |

Part A needs no Azure resources at all. Run it first: if the printer cannot
render what the app produces, nothing in Part B can work, and you will have spent
a deployment finding that out.

> **Read the paper, not the exit code.** Universal Print reports `completed` for
> a page that came out cropped, scaled or blank. Every "did it work?" in this
> document means *look at the sheet*.

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

Expect **357 passed** in about half a second. Red here means stop — do not spend
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

# Part B — Against the deployed app, from a local command window

`scripts\test.py` is the **same harness** for the local host and the deployed
app; only `--base-url` and `--key` change. That is deliberate — two harnesses
drift, and the local one becomes the one that lies.

## B1. Get the host name and a function key

Flex Consumption gives the app a **hashed** default hostname, so do not guess it:

```powershell
$RG  = "rg-noble-print"
$APP = "func-noble-print"

$HOST_NAME = az functionapp show --resource-group $RG --name $APP `
    --query "defaultHostName" -o tsv
$BASE = "https://$HOST_NAME"
$BASE

$KEY = az functionapp keys list --resource-group $RG --name $APP `
    --query "functionKeys.default" -o tsv
```

> **`az` and the corporate TLS proxy.** Plain `az` fails with
> `CERTIFICATE_VERIFY_FAILED` unless it goes through the truststore wrapper. In an
> interactive PowerShell session the profile function handles it. Anywhere else,
> call it by absolute path:
> ```powershell
> & 'C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe' -B "$env:LOCALAPPDATA\az-truststore\azrun.py" functionapp show --resource-group $RG --name $APP
> ```
> See `docs/ai/troubleshooting.md`.

Never paste a function key into a file that gets committed.

## B2. The validation path — must 400, must touch nothing

```powershell
.\.venv\Scripts\python.exe scripts\test.py badpayload --base-url $BASE --key $KEY
```

Expect `HTTP 400 (expected 400)` and `OK: rejected without touching the queue.`
A malformed request must be refused **before** anything is claimed, or a bad call
would lock files out until its own retry.

## B3. Dry run — resolves everything, prints nothing

```powershell
.\.venv\Scripts\python.exe scripts\test.py dryrun --base-url $BASE --key $KEY `
    --library "AI_DropBox_V2026" --folder "/Backup/Invoice" `
    --printer-share-id 4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5
```

This is the highest-value call in Part B. Expected shape:

```
HTTP 200
library      : Documents
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

## B4. One real file

```powershell
.\.venv\Scripts\python.exe scripts\test.py submit --base-url $BASE --key $KEY `
    --library "AI_DropBox_V2026" --folder "/Backup/Invoice" `
    --printer-share-id 4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5 --batch-size 1
```

`--batch-size 1` is not caution for its own sake: if the configuration is wrong
you have wasted one sheet rather than five.

Then **open the SharePoint library** and read the four columns back. This is the
step people skip, and it is the one that catches a silent write failure:

| Column | Expect |
|---|---|
| `Print_Status` | `PRINT_PENDING` |
| `Printer_Name` | the **share** id you passed |
| `Print_JobId` | a short number — job ids start at 1 per printer, not a GUID |
| `Print_Message` | empty |

## B5. Mark it complete once the page is out

```powershell
.\.venv\Scripts\python.exe scripts\test.py status --base-url $BASE --key $KEY `
    --library "AI_DropBox_V2026" --folder "/Backup/Invoice"
```

Then read the columns again: `Print_Status` = `PRINT_COMPLETED` and
`Print_Message` starting `printed on `. `Print_JobId` must survive — it is the
audit trail.

## Other Part B commands

```powershell
# What Resubmit would rescue (PENDING/FAILED older than the min age)
.\.venv\Scripts\python.exe scripts\test.py resubmit --base-url $BASE --key $KEY `
    --library "AI_DropBox_V2026" --folder "/Backup/Invoice" `
    --printer-share-id 4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5

# Files past the status window that nothing will touch again
.\.venv\Scripts\python.exe scripts\test.py stale --base-url $BASE --key $KEY `
    --library "AI_DropBox_V2026" --folder "/Backup/Invoice"

# Raw JSON instead of the summary
... --json
```

## Part B troubleshooting

| Symptom | Cause and fix |
|---|---|
| `could not reach ...` | Wrong host name. Flex hostnames are hashed — read `defaultHostName`, do not construct it |
| `HTTP 401` | Missing or wrong `--key` |
| `remedy: run scripts/bootstrap_token.py` | The delegated refresh token is dead. A password change or reset revokes it; expiry alone does not |
| `remedy: add the missing column(s)` | The four columns are missing or renamed |
| Every query fails | `Print_Status` is not indexed. A non-indexed column cannot be used in a Graph `$filter` at all |
| `printerAvailable: false` | The printer is not accepting jobs. HTTP 200 with `failed=0`, so only that flag reveals it |
| `budgetExhausted: true` | The wall-clock budget ran out mid-batch. The remainder is picked up next run — expected under load, not an error |
| `!!` warning on an item | The document printed but its job id could not be recorded. Resubmit will print it again in 72 h unless you fix the column by hand |

## Running against the local host instead

Same harness, no `--base-url` or `--key` — it defaults to `http://localhost:7071`.
Requires `functionapp\local.settings.json` (copy the template), an Entra app
registration, and a bootstrapped refresh token:

```powershell
.\scripts\start-local.ps1          # separate window; Ctrl+C stops it
.\.venv\Scripts\python.exe scripts\test.py dryrun --library "AI_DropBox_V2026" `
    --folder "/Backup/Invoice" --printer-share-id 4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5
```

If local Graph calls fail with `CERTIFICATE_VERIFY_FAILED`, install `truststore`
into the venv — `scripts\localshim\sitecustomize.py` is a no-op without it:

```powershell
.\.venv\Scripts\python.exe -m pip install truststore
```

A local run still cannot prove managed identity, RBAC, application settings,
packaging or cold start. See `README.md`, "What a local run cannot prove".
