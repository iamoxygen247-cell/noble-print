# noble-print

Azure Function App that prints SharePoint documents through Microsoft Universal
Print, driven by Power Automate. The SharePoint library is both the work queue
and the audit trail: four columns on each file carry its state.

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

# 5. Dry run -- resolves the site, library, the four INTERNAL column names and
#    the printer's real capabilities. Claims nothing, prints nothing.
.\.venv\Scripts\python.exe scripts\test.py dryrun `
    --library "Documents" --folder "/Invoices/ToPrint" `
    --printer-share-id "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5"

# 6. One real file, then mark it complete once the page is out.
.\.venv\Scripts\python.exe scripts\test.py submit `
    --library "Documents" --folder "/Invoices/ToPrint" `
    --printer-share-id "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5" --batch-size 1
.\.venv\Scripts\python.exe scripts\test.py status `
    --library "Documents" --folder "/Invoices/ToPrint"
```

**Then open the library and read the four columns.** A 200 response is not proof
the write landed.

## The two endpoints

| Name | Route | What it does |
|---|---|---|
| **Submit** | `POST /api/print/submit` | Claims the oldest `PRINT_READY` files and creates print jobs |
| **Poll** | `POST /api/print/status` | Checks `PRINT_PENDING` jobs: marks the finished, requeues the stalled, fails the hopeless |

Poll owns recovery. A stalled job is cancelled and the file handed back to
`PRINT_READY` on an exponential schedule — retry *n* becomes due at
`5 × (2ⁿ − 1)` minutes from the file's creation, so 5, 15, 35, 75 … — up to 10
retries, then `PRINT_FAILED` after 10 days. Poll runs every 10 minutes, so the
first requeue is observed at ≈10 min rather than ≈5. The pacing lives
in the Power Automate request body (`stallMinutes`, `maxRetries`, `giveUpDays`),
so it is retuned without a deploy.

> A third endpoint, `POST /api/print/resubmit`, ran daily and would not touch a
> file until it was 72 hours old. It was retired on 2026-09-01.

## The columns

| Column | Meaning |
|---|---|
| `Print_Status` | `PRINT_READY` → `PRINT_PENDING` → `PRINT_COMPLETED` / `PRINT_FAILED` |
| `Print_JobId` | The Universal Print job id. Only meaningful together with `Printer_Name` — job ids are per-printer, not globally unique |
| `Printer_Name` | The printer **share id** the job was sent to |
| `Print_Message` | `printed on YYYY-MM-DD HH:MM:SS` on success, or the failure reason, tagged with the stage that failed |

`PRINT_READY` is set by an upstream process. This app never creates work.

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

- **`Print_Status` must be indexed in SharePoint.** A non-indexed column cannot be
  used in a Graph `$filter` at all, so without the index every query fails.
- Entra app registration with public client flows enabled, and admin consent for
  the delegated scopes `Sites.ReadWrite.All`, `PrintJob.ReadWriteBasic`,
  `Printer.Read.All`, `PrinterShare.ReadBasic.All`, `offline_access`.
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
    --folder "/Invoices/ToPrint" `
    --printer-share-id "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5"
```

## This tenant's printer

| | |
|---|---|
| Printer | **Brother MFC-L5800DW series [3c2af401eecf]** (registered 2026-08-31) |
| **Share Id** | `4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5` — pass this as `printerShareId`; it addresses jobs |
| **Printer Id** | `cf8d9fa1-0502-4b0d-b28e-22a52cdde8a1` — used only to cancel a job; the app resolves it itself |

> ## This printer takes raster only — the app converts
>
> It reports **`image/pwg-raster` and nothing else** (verified against the live
> API). Universal Print will not convert for us: it performs exactly one
> conversion, **OXPS → PDF**, and only for printers that already accept PDF. So
> the app rasterizes each PDF before upload — see **Printer profiles** below.
>
> The predecessor, a Brother DCP-L2540DW, accepted `application/pdf` directly. It
> was retired because it is not a Universal Print ready device, and its
> identifiers have been removed from this repo.

They are different GUIDs and are **not** interchangeable. Jobs live at
`/print/shares/{shareId}/jobs`, but cancel is documented only at
`/print/printers/{printerId}/jobs/{jobId}/cancel`. Sending the share id to the
cancel route returns 404, which reads as "already gone" — so the original job
survives and prints alongside its replacement. That was a real defect (F3); the
regression lives in `tests/test_real_printer.py`.

### Its capabilities and defaults (content types verified against the live API, 2026-08-31)

| Setting | Value | Why it matters here |
|---|---|---|
| **Content types (capability)** | **`image/pwg-raster` — only** | Read from the API, not the portal. The document must be rasterized before upload; see the warning above |
| Colour mode (default) | Grayscale | Device is mono |
| DPI | default 600; **300 also supported** | 300 is the conversion target: ~8.4 MB/page raw at Letter versus ~33.7 MB at 600 |
| Copies per job | 1 | Matches the only setting we send, so they can never disagree |
| Duplex mode | **None** | Single-sided: a 40-page batch is 40 sheets. A printer-side setting, not a code change |
| Fit PDF to page / Multipage layout / Pages per impression | *greyed out* | Unsupported. `JOB_CONFIGURATION` must never send them — a test enforces that |

> The rows above other than content types come from the portal's **Printer
> defaults** page, which shows defaults rather than the capability list. Those two
> are different fields, and confusing them is what made an earlier version of this
> table wrong for the previous printer. Read capabilities from the API.
>
> **`dpi` is deliberately not sent in `printJobConfiguration`.** For a
> pre-rasterized document the resolution that matters is the one written into the
> PWG page header; a job-level value that disagrees invites the printer to rescale
> a bitmap that is already correct.

The app sends **only** `{"copies": 1}` and lets these device defaults decide the
rest. That is why changing duplex or colour is a printer setting, not a release.

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
