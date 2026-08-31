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
    --printer-share-id "a11f0263-68b7-45f4-b042-f1b4b30b60a3"

# 6. One real file, then mark it complete once the page is out.
.\.venv\Scripts\python.exe scripts\test.py submit `
    --library "Documents" --folder "/Invoices/ToPrint" `
    --printer-share-id "a11f0263-68b7-45f4-b042-f1b4b30b60a3" --batch-size 1
.\.venv\Scripts\python.exe scripts\test.py status `
    --library "Documents" --folder "/Invoices/ToPrint"
```

**Then open the library and read the four columns.** A 200 response is not proof
the write landed.

## The three endpoints

| Name | Route | What it does |
|---|---|---|
| **Submit** | `POST /api/print/submit` | Claims the oldest `PRINT_READY` files and creates print jobs |
| **Poll** | `POST /api/print/status` | Checks `PRINT_PENDING` jobs and marks the finished ones |
| **Resubmit** | `POST /api/print/resubmit` | Cancels and retries outstanding jobs older than 72 hours |

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
    --printer-share-id "a11f0263-68b7-45f4-b042-f1b4b30b60a3"
```

## This tenant's printer

| | |
|---|---|
| Printer | Brother DCP-L2540DW series |
| **Share Id** | `a11f0263-68b7-45f4-b042-f1b4b30b60a3` — pass this as `printerShareId`; it addresses jobs |
| **Printer Id** | `ffa65a34-615c-493b-9eff-d227133293ac` — used only to cancel a job; the app resolves it itself |

They are different GUIDs and are **not** interchangeable. Jobs live at
`/print/shares/{shareId}/jobs`, but cancel is documented only at
`/print/printers/{printerId}/jobs/{jobId}/cancel`. Sending the share id to the
cancel route returns 404, which reads as "already gone" — so the original job
survives and prints alongside its replacement. That was a real defect (F3); the
regression lives in `tests/test_real_printer.py`.

### Its capabilities and defaults (verified against the live API, 2026-08-30)

| Setting | Value | Why it matters here |
|---|---|---|
| **Content types (capability)** | `application/pdf`, `application/oxps` | Read from the API, not the portal. PDFs pulled from SharePoint go straight to the device — no OXPS conversion needed |
| Content type (default) | `application/pdf` | A *different* field from the list above. The portal's Properties page shows only this one, which is what made an earlier version of this table wrong |
| Copies per job | 1 | Matches the only setting we send, so they can never disagree |
| Colour mode | Grayscale | Device is mono-only (`isColorPrintingSupported: false`) |
| Duplex mode | **None** | Single-sided: a 40-page batch is 40 sheets. A printer-side setting, not a code change |
| Fit PDF to page / Multipage layout | *greyed out* | Unsupported. `JOB_CONFIGURATION` must never send them — a test enforces that |

The app sends **only** `{"copies": 1}` and lets these device defaults decide the
rest. That is why changing duplex or colour is a printer setting, not a release.

### No connector — settled, and correct

**This printer is Universal Print ready and registered directly.** Confirmed
2026-08-30 by measurement, not inference: `live-printer-check.ps1` submitted a
real job while the API reported **0 connectors on this printer and 0 across the
whole tenant**, and a page came out of the tray — job `6`, `pending` →
`processing` → `completed` in about five seconds. Nothing could have been
bridging it, so the service is talking to the device itself.

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
