# noble-print

Azure Function App that prints SharePoint documents through Microsoft Universal
Print, driven by Power Automate. The SharePoint library is both the work queue
and the audit trail: five columns on each file carry its state.

Full design: **[docs/design.md](docs/design.md)**. Inherited engineering rules:
[docs/ai/project-playbook.md](docs/ai/project-playbook.md).

## Runbooks

| Doc | When |
|---|---|
| **[docs/live-test.md](docs/live-test.md)** | Testing against the real printer and SharePoint. **Start here.** |
| **[docs/deploy-to-azure.md](docs/deploy-to-azure.md)** | Deploying to production, step by step |
| [docs/design.md](docs/design.md) | Why it works the way it does — requirements trace, write matrix, reporting |
| [docs/ai/troubleshooting.md](docs/ai/troubleshooting.md) | Confirmed mistakes and verified fixes. Read before debugging |

## Local test in six commands

Full detail — including the SharePoint and Entra prerequisites — is in
[docs/live-test.md](docs/live-test.md). The short version, once those are set up:

```powershell
cd "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"

# 1. Offline suite. No cloud account, no network, ~0.3s.
.\.venv\Scripts\python.exe -m pytest

# 2. Does the printer physically print? No Function App, no SharePoint,
#    no app registration -- signs in with device code and prints one page.
.\scripts\live-printer-check.ps1

# 3. One-time: get a refresh token for local runs.
.\.venv\Scripts\python.exe scripts\bootstrap_token.py --print-only
#    paste the value into PRINT_REFRESH_TOKEN in functionapp\local.settings.json

# 4. Start the host (venv activation + TLS shim + func start). Leave it running.
.\scripts\start-local.ps1
```

Then in a **second** PowerShell window:

```powershell
cd "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"

# 5. Dry run -- resolves the site, library, the five INTERNAL column names and
#    the printer's real capabilities. Claims nothing, prints nothing.
.\.venv\Scripts\python.exe scripts\test.py dryrun `
    --hostname noblehomes.sharepoint.com --site-path /sites/PM `
    --library "Documents" --folder "/Invoices/ToPrint" `
    --printer-share-id "5f488e73-ab80-4a6b-a60a-a0f883e17e2e"

# 6. One real file, then mark it complete once the page is out.
.\.venv\Scripts\python.exe scripts\test.py submit `
    --hostname noblehomes.sharepoint.com --site-path /sites/PM `
    --library "Documents" --folder "/Invoices/ToPrint" `
    --printer-share-id "5f488e73-ab80-4a6b-a60a-a0f883e17e2e" --batch-size 1
.\.venv\Scripts\python.exe scripts\test.py status `
    --hostname noblehomes.sharepoint.com --site-path /sites/PM `
    --library "Documents" --folder "/Invoices/ToPrint"
```

**Then open the library and read the five columns.** A 200 response is not proof
the write landed.

## The three endpoints

| Name | Route | What it does |
|---|---|---|
| **Health** | `POST /api/print/health` | Can the pipeline work right now? One share read, **no writes**. Called before the other two |
| **Submit** | `POST /api/print/submit` | Claims the oldest `PRINT_READY` files **that are due** and creates print jobs |
| **Poll** | `POST /api/print/status` | Checks `PRINT_PENDING` jobs: marks the finished, requeues the stalled, fails the hopeless. A job the printer has not taken yet is left alone |

Submit and Poll both require the site in the request body —
`sharepointHostname` and `sharepointSitePath`, alongside `library` and `folder`.
These were app settings until 2026-09-02; one Function App now serves any site a
flow names. Health takes neither, because it resolves no library.

Poll owns recovery. A stalled job is cancelled and the file handed back to
`PRINT_READY` with **`Print_Time`** set to the moment the next attempt is due —
retry *n* falls due at `5 × (2ⁿ − 1)` minutes from the file's creation, so
5, 15, 35, 75 … — and Submit will not claim it before then. `giveUpDays` (10) is
the only bound: the schedule is clamped to it, and past it the row is cancelled
and written `PRINT_FAILED`. The pacing lives in the Power Automate request body
(`stallMinutes`, `giveUpDays`) and nowhere else, so it is retuned without a deploy.

**A job the printer has not taken is never "stalled" (R24).** Universal Print's
`pending` means the device has not started it, so there is nothing stuck to
cancel — cancelling would kill a document waiting its turn — and `giveUpDays` is
the only bound on such a row. Every other non-terminal state stalls as before, and
the threshold measures how long the *printer* has held the job:
`acknowledgedDateTime` where there is a usable one, otherwise `createdDateTime`.

> **Reading a live run:** the *waiting* is exact, but the front of the ladder is
> skipped, and it is **Flow A's** recurrence that decides how much — `spent` is
> read off the file's age when its *first* job is created. Flow A every 15 min
> opens the schedule at retry 3. Flow B's cadence no longer costs a retry at all.

> **`POST /api/print/resubmit` is gone.** It ran daily and would not touch a file
> until it was 72 hours old; it was retired on 2026-09-01 and its recovery work
> folded into Poll. Health is not its replacement — it reads a printer and writes
> nothing.

## The columns

| Column | Meaning |
|---|---|
| `Print_Status` | `PRINT_READY` → `PRINT_PENDING` → `PRINT_COMPLETED` / `PRINT_FAILED` |
| `Print_JobId` | The Universal Print job id. Only meaningful together with `Printer_Name` — job ids are per-printer, not globally unique |
| `Printer_Name` | The printer **share id** the job was sent to |
| `Print_Message` | `printed on YYYY-MM-DD HH:MM:SS` on success, or the failure reason, tagged with the stage that failed |
| `Print_Time` | **When the next print attempt falls due.** Set only on a `PRINT_READY` row, and cleared everywhere else. A **Date and Time** column; it needs no index |

`PRINT_READY` is set by an upstream process. This app never creates work.

**`Print_Time` is the retry backoff, written down.** When Poll finds a stalled job
it cancels it, sets the row back to `PRINT_READY`, and records the moment the next
attempt is due; Submit will not claim the file until then. A value in the past
means "print now", and **an empty value means the same** — files arrive from
upstream without one, and a row a human resets from `PRINT_FAILED` has none either,
so a blank has to mean *go*, not *wait forever*.

**`Print_Message` on success records the printer's own `acknowledgedDateTime` —
when the printer took the job — not the instant the page finished.** `printJob`
has no completion field, so no exact answer exists; this is the closest one that
belongs to the *job* rather than to our polling schedule. On the job measured
2026-08-30 it landed about ten seconds before the page was done, where the
previous value — the moment Poll happened to look — could be a full polling
interval late and moved whenever the schedule changed.

If a job carries no acknowledgement, the observed time is used instead. A missing
optional field must never cost the status write and strand the file.

## Why a service account, not managed identity

Universal Print's job APIs are **delegated-only**. Creating, starting and
cancelling a job are all documented `Application: Not supported`, and
`createUploadSession` on a printer share is "supported with delegated
permissions only". A Function App has no signed-in user, so a print service
account signs in once interactively and its refresh token lives in Key Vault:

```powershell
.\.venv\Scripts\python.exe scripts\bootstrap_token.py
```

Run that once per environment, and again whenever the token is revoked — a
password change, an SSPR, an admin reset, or an explicit revocation will do it.
Password *expiry* alone will not. When it breaks, every endpoint returns 500 with
`"remedy": "run scripts/bootstrap_token.py"`.

## Prerequisites

- **The five columns must exist**, with those exact display names. `Print_Time` is
  a **Date and Time** column (Include Time on, no default value); the other four
  are single line of text. A missing one is a loud 500 from Submit and Poll naming
  the column.
- **`Print_Status` must be indexed in SharePoint.** A non-indexed column cannot be
  used in a Graph `$filter` at all, so without the index every query fails.
  `Print_Time` must **not** need an index — SharePoint honours only one indexed
  field per `$filter`, so the due-time comparison happens in Python instead.
- Entra app registration with public client flows enabled, and admin consent for
  the delegated scopes `Sites.ReadWrite.All`, `PrintJob.ReadWriteBasic`,
  `PrintJob.Create`, `Printer.Read.All`, `PrinterShare.ReadBasic.All`,
  `offline_access`. Both PrintJob scopes are required — `createUploadSession`
  accepts `PrintJob.Create` or `PrintJob.ReadWrite` but not `ReadWriteBasic`.
- The Function App's managed identity needs **Key Vault Secrets Officer** —
  *Officer*, not *User*, because rotation writes the new token back.

## Local development

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r functionapp\requirements.txt
.\.venv\Scripts\python.exe -m pytest                # offline suite, no cloud account

Copy-Item functionapp\local.settings.json.template functionapp\local.settings.json
# fill it in, then:
.\scripts\start-local.ps1
.\.venv\Scripts\python.exe scripts\test.py dryrun --library "Documents" `
    --hostname noblehomes.sharepoint.com --site-path /sites/PM `
    --folder "/Invoices/ToPrint" `
    --printer-share-id "5f488e73-ab80-4a6b-a60a-a0f883e17e2e"
```

## This tenant's printer

| | |
|---|---|
| Printer | **Noble Home MFC** |
| **Share Id** | `5f488e73-ab80-4a6b-a60a-a0f883e17e2e` — pass this as `printerShareId`; it addresses jobs |
| **Printer Id** | *(read it from the share — the app resolves it itself)* |
| Content types | **unrecorded — read them, see below** |

> ## ⚠️ Read this printer's content types — they change what the app does
>
> Universal Print will not convert for us: it performs exactly one conversion,
> **OXPS → PDF**, and only for printers that already accept PDF. So whether the
> app rasterizes is decided by what the device reports — and so is what an
> omitted `printFormat` selects, because `PwgRasterProfile.matches` stands aside
> as soon as the printer accepts PDF (see **Printer profiles** below):
>
> | Reports | No `printFormat` | `printFormat: image/pwg-raster` |
> |---|---|---|
> | `image/pwg-raster` only | `pdf-to-pwg-raster` | `pdf-to-pwg-raster` |
> | **both** | **`passthrough`** — PDF uploaded unconverted | `pdf-to-pwg-raster` |
>
> ```powershell
> .\scripts\live-printer-check.ps1 -DiagnoseOnly    # prints the real list
> ```
>
> **Record the answer here with a date**, then set Flow A's body to match —
> `docs/deploy-to-azure.md` step 10 has the two branches.
>
> **Do not carry the previous printer's answer over.** This share replaced the
> **Brother MFC-L5800DW series [3c2af401eecf]** (share `4429bf4e-…`, printer
> `cf8d9fa1-…`), which reported `image/pwg-raster` and nothing else — verified
> 2026-08-31, on a device that is no longer the target. Its predecessor in turn, a
> Brother DCP-L2540DW, accepted `application/pdf` directly and was retired for not
> being Universal Print ready.

They are different GUIDs and are **not** interchangeable. Jobs live at
`/print/shares/{shareId}/jobs`, but cancel is documented only at
`/print/printers/{printerId}/jobs/{jobId}/cancel`. Sending the share id to the
cancel route returns 404, which reads as "already gone" — so the original job
survives and prints alongside its replacement. That was a real defect (F3), and
the share-id-vs-printer-id half of it is fixed for good:
`tests/test_poll.py::test_the_cancel_uses_the_printer_id_not_the_share_id`.

The **wrong-printer** half is live again by choice. Poll accepts an optional
`printerShareId` that hard-overrides each row's `Printer_Name`, so the two can
disagree once more. Exposure is zero while one printer is registered, and a
disagreement is counted as `printerOverridden` with a WARNING. Tracked as **F3-R**
in `docs/ai/open-defects.md`; the row-follows-its-own-printer default is pinned by
`tests/test_poll.py::test_the_cancel_uses_the_printer_named_on_the_row`.

### Its capabilities and defaults

> **These rows were measured on the previous printer (share `4429bf4e-…`) and are
> kept as the shape of the answer, not as this device's answer.** Re-read them
> against `5f488e73-…` and re-date this heading.

| Setting | Value | Why it matters here |
|---|---|---|
| **Content types (capability)** | **re-read — was `image/pwg-raster` only** | Read from the API, not the portal. It decides whether the document is rasterized *and* what an omitted `printFormat` selects; see the warning above |
| Colour mode (default) | Grayscale | Device is mono |
| DPI | default 600; **300 also supported** | 300 is the conversion target: ~8.4 MB/page raw at Letter versus ~33.7 MB at 600 |
| Copies per job | 1 | Matches what both profiles send, so they can never disagree |
| Duplex mode | **None** | Single-sided: a 40-page batch is 40 sheets. A printer-side setting on the passthrough path; the raster profile sends `duplexMode: oneSided` explicitly |
| Fit PDF to page / Multipage layout / Pages per impression | *greyed out* | Unsupported. `JOB_CONFIGURATION` must never send them — a test enforces that |

> The rows above other than content types come from the portal's **Printer
> defaults** page, which shows defaults rather than the capability list. Those two
> are different fields, and confusing them is what made an earlier version of this
> table wrong for the previous printer. Read capabilities from the API.

**What the app actually sends depends on the profile, and the two differ sharply.**

| Profile | `printJobConfiguration` | So the device defaults… |
|---|---|---|
| `passthrough` | `{"copies": 1}` — `print_policy.JOB_CONFIGURATION` | …decide everything else. Duplex and colour are a printer setting, not a release |
| `pdf-to-pwg-raster` | **twelve keys**, including `dpi`, `orientation`, `duplexMode: oneSided`, `colorMode: grayscale`, `mediaSize`, `scaling` and `margin` (`printing/profiles.py`) | …are **overridden**. Changing duplex or colour on this path *is* a code change |

> **`scaling: fit` and `margin` are load-bearing on the raster path.** The
> converter renders full-bleed at the media size and does not inset the printer's
> unprintable margins; the job configuration compensates. Send different scaling,
> or no margins, and the page prints cropped — while Graph still reports
> `completed`. `dpi` is sent and **must equal the render dpi**, or the printer
> rescales a bitmap that is already correct.

### No connector — settled for the old printer, not yet for this one

**Not yet proven for the MFC-L5800DW.** The 2026-08-31 check reported **0
connectors on the printer and 0 across the tenant**, with the device `idle` and
accepting jobs — but that is exactly the evidence this section goes on to explain
is *insufficient on its own*. Nothing has been printed on it. The question stays
open until a page comes out, which cannot happen before the raster conversion
exists.

**It was settled for the retired DCP-L2540DW**, and the reasoning is kept because
it is what makes the result mean anything. Confirmed 2026-08-30 by measurement,
not inference: `live-printer-check.ps1` submitted a real job while the API
reported **0 connectors on that printer and 0 across the whole tenant**, and a
page came out of the tray — job `6`, `pending` → `processing` → `completed` in
about five seconds. Nothing could have been bridging it, so the service was
talking to the device itself.

**What that buys you operationally:** there is no Windows host in this path.
Nothing to keep powered on, nothing to patch, and no single point of failure
between Universal Print and the printer. A connector-backed printer would have
had all three.

Two earlier answers here were wrong, and the way they were wrong is worth
keeping. The first claimed the direct path was confirmed by the Overview blade —
*last seen 2 minutes ago, Ready, accepting jobs*. It was not: **a connector's
heartbeat updates last-seen exactly as a native printer's does**, so those
signals fit both cases equally. The second treated 0 connectors as decisive; it
is not, for the same reason — 0 connectors is what you see both when none is
needed and when one is missing. Only paper in the tray separated them.

The app deliberately **does not** check connectors. It would need the
`PrintConnector.Read.All` scope and tells you less than `isAcceptingJobs` and
`status.state`, which the preflight already reads. If you ever want the check by
hand, the sibling project's `docs/gitignore/printer_MFCL5800DW.txt` step 9 has it.

`local.settings.json` is **never deployed** — the cloud app reads Application
Settings. "It works locally" is not evidence about the deployed app.

`PRINT_REFRESH_TOKEN` in `local.settings.json` is a local escape hatch so
`func start` works without Key Vault access. It logs a warning on every use, so
if it ever reaches Azure the evidence is in Application Insights.

## Printer profiles — how a document is prepared

`functionapp/printing/` decides what to upload for a given printer. A **profile**
pairs a conversion with the job configuration that makes its output print
correctly, because those two are one unit and not separable:

| Profile | When it runs | Uploads |
|---|---|---|
| `passthrough` | the printer already accepts the document | the original bytes, `{"copies": 1}` |
| `pdf-to-pwg-raster` | the printer reports `image/pwg-raster` but not PDF | an 8-bit greyscale PWG raster at 300 dpi, plus the settings below |

The raster is rendered **full-bleed at the exact media size** — US Letter at
300 dpi is always 2550×3300 — and deliberately does *not* inset the printer's
unprintable margins. The job configuration compensates with `scaling: fit` and the
device's reported margins. Change one without the other and pages print cropped;
`tests/test_printing.py` pins the pair.

```
functionapp/printing/
├── __init__.py        select_profile() and the PROFILES registry
├── pwg_converter.py   PDF -> PWG 5102.4, with its own independent validator
├── profiles.py        the profiles and their job configuration
├── sender.py          convert, then hand to universal_print.py
└── __main__.py        python -m printing INPUT.pdf OUTPUT.pwg --dpi 300
```

**Adding a printer** is a new profile class plus one entry in `PROFILES`. No route
changes: `function_app.py` asks for a profile and never branches on a MIME type.

Rendering uses **pypdfium2** (BSD-3/Apache-2.0, self-contained manylinux wheels).
The PWG encoding is standard library only. Nothing native is installed on the
host, which matters because Flex Consumption has no custom-container path.

`python -m printing` produces a `.pwg` on disk for bench-testing a printer without
SharePoint, Graph or the Function App. `scripts/test.py dryrun` reports which
profile a share would select and the exact job configuration it would send —
use it to find out a printer needs conversion *before* queueing any files.

## What a local run cannot prove

| Not covered locally | Why |
|---|---|
| Managed identity + RBAC | Locally you are your `az login` principal, with *your* roles |
| Application settings | `local.settings.json` never leaves the machine |
| Packaging | `.funcignore` correctness only shows up at publish time |
| Cold start, plan limits, platform timeouts | Not modelled by the local host |
| Power Automate's ~120 s connector budget | A property of the caller, not of your host |
| Whether the printer actually printed | Only a physical tray can tell you that |

## Reporting

Current state — how many are pending, failed, completed **right now** — is a
SharePoint question: group a library view by `Print_Status`.

Activity over time — how many were submitted, retried or completed **each week**
— cannot come from SharePoint, because the columns hold only the latest state (a
file retried three times looks identical to one retried once). That comes from
the `PRINT_EVENT` log line in Application Insights. Queries and the workbook
build are in [docs/design.md §13](docs/design.md).

**Sampling must stay disabled in `host.json`.** Adaptive sampling drops `traces`
rows with no error anywhere, which would make every count quietly wrong.
