# Live test runbook

Two tests, in this order. **Stage 1 needs nothing but PowerShell** — no Function
App, no SharePoint, no Entra app registration — and it answers the question the
Azure portal left ambiguous. Do it first; if it fails, nothing in stage 2 can work.

---

## Stage 1 — does this printer actually print?

> **Already run, 2026-08-30 — and it printed.** This printer is **Universal Print
> ready and registered directly**: 0 connectors on the printer and 0 tenant-wide,
> job `6` went `pending` → `processing` → `completed` in about five seconds, and a
> page came out. There is no Windows host in the path. Re-run this stage when the
> printer, the share or the tenant changes; the reasoning below is kept because it
> is what makes the result mean something.

### What it settles

The Connectors blade reads *"No rows to display"*, which on its own is
**ambiguous**:

| If the printer is… | Then no connector is… | And it prints? |
|---|---|---|
| Universal Print **ready** (native UP firmware) | expected — it registers directly | yes |
| **not** UP-ready | a problem — nothing bridges it to the device | **no** — jobs sit in `pending` forever |

The Overview blade does **not** distinguish these. *"Last seen: 2 minutes ago"*
updates from a connector's heartbeat **and** from a native printer's, so a live
look is consistent with either. The only way to tell without guessing is to
submit a job and watch what happens to it.

> The Brother DCP-L2540DW is a 2018 entry-level SOHO laser. Enterprise devices
> are the usual UP-ready ones, so a native registration would be a little
> surprising here — which is exactly why this is worth measuring rather than
> assuming.

### Run it

```powershell
cd "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"

# 1. Diagnostics only -- signs in, reads the share, the printer and the
#    connectors. Submits nothing, prints nothing.
.\scripts\live-printer-check.ps1 -DiagnoseOnly

# 2. The real thing -- generates a one-page PDF, runs the same four Graph calls
#    the Function App makes, then polls the job to a terminal state.
.\scripts\live-printer-check.ps1
or 
.\scripts\live-printer-check.ps1 -PdfPath "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print\samples\260607_0002.pdf"
```

It signs in with **device code** using the Microsoft Graph PowerShell first-party
app, so there is no app registration to create. It is the same path already
validated in the sibling project's `docs/gitignore/printer_MFCL5800DW.txt`.

Sign in as whoever should own print jobs. To print your own PDF instead:

```powershell
.\scripts\live-printer-check.ps1 -PdfPath "C:\path\to\invoice.pdf"
```

**Where the test page goes.** With no `-PdfPath` the script generates a fresh one
into your TEMP directory and never touches the repo. Generating it each time is
deliberate: it keeps the document from becoming the variable when a print fails.
To keep a copy instead:

```powershell
.\.venv\Scripts\python.exe scripts\make_test_pdf.py   # -> samples\noble-print-test.pdf
.\scripts\live-printer-check.ps1 -PdfPath ".\samples\noble-print-test.pdf"
```

`samples/` is gitignored — the playbook's home for real inputs and generated
artifacts — so anything kept there stays local to your machine.

### Reading the result

| Verdict | Meaning | Next |
|---|---|---|
| `completed` **and** 0 connectors | The printer is UP-ready and registered directly. Question settled, favourably. | Stage 2 |
| `completed` **and** ≥1 connector | Connector-backed. Works — but that Windows host is a single point of failure: if it is off, everything sits in `PRINT_PENDING`. | Stage 2, and note the dependency |
| stuck in `pending` past the timeout | **Universal Print accepted the job but nothing delivered it.** This is the failure the connector question was about. | Fix registration before anything else |
| `canceled` / `aborted` | The job reached the device and was rejected. | Read the status description |

The script also prints the **content types the printer reports to the API**,
which is a fuller answer than the portal's Properties tab — that shows only the
default. If `application/pdf` is absent, every submission fails at preflight and
no code change can help: PDF cannot be converted to OXPS.

> **One scope note.** `live-printer-check.ps1` asks for `PrintConnector.Read.All`
> so it can answer the registration question. It may need admin consent. **The
> Function App deliberately does not request it** — `isAcceptingJobs` and
> `status.state` answer the operational question and need no extra consent.
> If consent is refused, the connector step is skipped and printing still works.

---

## Between the stages — measure the job retention window

The one remaining unknown, and the only one that can still cause a duplicate
print (UC-9). If Universal Print discards a finished job before Poll sees it,
Poll gets a 404, writes nothing, and Resubmit reprints the document 72 hours
later.

Stage 1 left a **completed job `6`** behind, so measuring costs one call a day
and nothing on paper — but **read the note below before trusting a 404.**

```powershell
# The printer route, not the share route. Job ids are per-PRINTER, and the
# printer id survives a re-share; the share id does not.
Invoke-MgGraphRequest -Method GET `
  -Uri "https://graph.microsoft.com/v1.0/print/printers/ffa65a34-615c-493b-9eff-d227133293ac/jobs/6"
```

The **last day this still returns a job** is the retention window. Set Flow B's
interval well inside it. Completion itself is fast — job `6` finished in about
five seconds — so the danger is never polling too early, only polling after the
record is gone.

> **The 2026-08-31 re-share voided the measurement in progress.** The printer
> share was deleted and re-created, minting share id
> `a11f0263-68b7-45f4-b042-f1b4b30b60a3` in place of `5de37377-…`. Job `6` was
> created through the old share, so a 404 now says only "that share is gone" —
> it says nothing about retention, and reading it as an answer would set Flow B's
> cadence from a false measurement. If the printer-route call above also 404s,
> the run is not evidence: restart the count from the next job printed through
> the new share. This is also why the call was changed off the share route.

**Log what you find here:**

| Checked | Job `6` still returns? |
|---|---|
| 2026-08-30, ~15 min after printing | yes |
| 2026-08-31 | *(share re-created — measurement restarted, see above)* |
| _(next check)_ | |

> One reading proves only that retention is longer than fifteen minutes, which
> was never in doubt. The answer needs days, so check once a day until it 404s.
> Until then Flow B's ten-minute cadence is a **guess** — a safe-looking one, but
> UC-9's duplicate print is exactly what an unlucky guess costs.

---

## Stage 2 — the whole pipeline

Only worth doing once stage 1 prints.

### Prerequisites

**A — SharePoint.** In the target library:

1. The four columns exist with these **exact display names**:
   `Print_Status`, `Print_JobId`, `Print_Message`, `Printer_Name` (all single
   line of text).
2. **`Print_Status` is indexed.** *Library settings → Indexed columns → Create a
   new index.* Not optional: a non-indexed column cannot be used in a Graph
   `$filter` at all, so without it **every** query fails.
3. At least one PDF in the target folder with `Print_Status = PRINT_READY`.

**B — Entra app registration** (this one you do need, unlike stage 1):

1. New registration → **Authentication** → enable **Allow public client flows**.
2. **API permissions** → Microsoft Graph → *Delegated*:
   `Sites.ReadWrite.All`, `PrintJob.ReadWriteBasic`, `Printer.Read.All`,
   `PrinterShare.ReadBasic.All`, `offline_access` → **Grant admin consent**.
3. Note the **Application (client) ID** and **Directory (tenant) ID**.

**C — local settings:**

```powershell
Copy-Item functionapp\local.settings.json.template functionapp\local.settings.json
notepad functionapp\local.settings.json
```

Fill in `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `SHAREPOINT_HOSTNAME`
(e.g. `noblehomes.sharepoint.com`), `SHAREPOINT_SITE_PATH` (e.g. `/sites/Operations`).

For a local run you can skip Key Vault entirely — leave `KEY_VAULT_URI` as the
placeholder and put the refresh token straight in `PRINT_REFRESH_TOKEN`:

```powershell
.\.venv\Scripts\python.exe scripts\bootstrap_token.py --print-only
```

Copy the value into `PRINT_REFRESH_TOKEN`. It logs a warning on every use so it
can never quietly reach Azure. `local.settings.json` is gitignored.

### Run it

```powershell
# 1. Offline suite first -- 353 tests, no cloud account, ~0.3s.
.\.venv\Scripts\python.exe -m pytest

# 2. Start the host (venv activation + TLS shim + func start).
.\scripts\start-local.ps1
```

Then, in a **second** PowerShell window:

```powershell
cd "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"

# 3. Validation path: must be 400, and must touch nothing.
.\.venv\Scripts\python.exe scripts\test.py badpayload

# 4. Dry run -- resolves everything and prints NOTHING. This is the step that
#    tells you the REAL internal column names, which is the one thing I could
#    not determine without your tenant.
.\.venv\Scripts\python.exe scripts\test.py dryrun `
    --library "Documents" --folder "/Invoices/ToPrint" `
    --printer-share-id "a11f0263-68b7-45f4-b042-f1b4b30b60a3"

# 5. One real file.
.\.venv\Scripts\python.exe scripts\test.py submit `
    --library "Documents" --folder "/Invoices/ToPrint" `
    --printer-share-id "a11f0263-68b7-45f4-b042-f1b4b30b60a3" `
    --batch-size 1

# 6. Mark it complete once the page is out.
.\.venv\Scripts\python.exe scripts\test.py status `
    --library "Documents" --folder "/Invoices/ToPrint"
```

### What "passing" looks like

Step 4 should list the four columns and their internal names. If they come back
as `Print_x005f_Status` etc., the encoding I predicted is real; if they come back
clean, it is not. **Either is fine** — the code resolves them at runtime — but
this is where you find out.

After step 5, open the library. The file should read:

| Column | Value |
|---|---|
| `Print_Status` | `PRINT_PENDING` |
| `Printer_Name` | `a11f0263-68b7-45f4-b042-f1b4b30b60a3` |
| `Print_JobId` | a short number — they start at 1 per printer, so expect something like `6`, not a GUID |
| `Print_Message` | *(empty)* |

After step 6, once printed:

| Column | Value |
|---|---|
| `Print_Status` | `PRINT_COMPLETED` |
| `Print_Message` | `printed on 2026-08-30 09:14:07` |

**Read the columns back — do not stop at a 200.** A response can look perfect
while the write silently failed; step 5 of the deploy smoke test exists for
exactly that reason.

---

## If something fails

| Symptom | Cause | Fix |
|---|---|---|
| 500, `remedy: run scripts/bootstrap_token.py` | Refresh token expired or revoked | Re-run the bootstrap |
| 500 naming a column | Display name mismatch in the library | Fix the column name, or tell me the real ones |
| Every query fails / times out | `Print_Status` is not indexed | Add the index |
| `candidatesFound: 0` but files are there | Folder path or status value mismatch | Check `dryrun`'s resolved folder; confirm the status text is exactly `PRINT_READY` |
| `does not accept application/pdf` | The printer reports no PDF support | Printer-side; stage 1 shows the real list |
| `PRINT_PENDING` forever, nothing prints | Stage 1's failure mode — nothing is delivering jobs | Fix the registration, not the code |
| Job id in the column but a 404 on status | The job aged out of Universal Print | Poll more often; Resubmit will retry after 72h |

Anything you hit that is not on this list is worth adding to
`docs/ai/troubleshooting.md` — that file is the project's memory.
