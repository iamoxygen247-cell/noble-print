# Deploy to Azure — production runbook

Step by step, in order. **The order matters**: several steps depend on the one
before, and two of them fail *silently* if done out of sequence.

> **Do [`docs/live-test.md`](live-test.md) first.** If the printer does not print
> from a standalone script, nothing here will make it print. That test also
> answers the one open question about this printer's registration.

---

## 0. What a "deploy" actually consists of

Seven independent artifacts. Only #5 is what people mean by "deploy", and
forgetting any of the others produces a working-looking app that does nothing.

| # | Artifact | Deployed by | Skipped ⇒ |
|---|---|---|---|
| 1 | Entra app registration + admin consent | portal / `az ad` | every call 500s on auth |
| 2 | SharePoint column index | SharePoint UI | **every query fails** |
| 3 | Azure resources + RBAC | `az` | app cannot read its own secret |
| 4 | Refresh token in Key Vault | `bootstrap_token.py` | every call 500s with a remedy message |
| 5 | Function code | `func … publish` | — |
| 6 | Application settings | `az … appsettings set` | app raises on missing config |
| 7 | Power Automate flows | Power Automate | nothing ever runs |

### Prerequisites on your machine

```powershell
az --version          # Azure CLI
func --version        # Azure Functions Core Tools v4
az login
az account set --subscription "<SUBSCRIPTION_NAME_OR_ID>"
```

### Names used below

Set these once and paste the rest verbatim.

```powershell
$RG        = "rg-noble-print"
$LOC       = "canadacentral"
$APP       = "func-noble-print"          # must be globally unique
$STORAGE   = "stnobleprint"              # 3-24 lowercase alphanumerics, globally unique
$VAULT     = "kv-noble-print"            # globally unique
$INSIGHTS  = "appi-noble-print"
$WORKSPACE = "log-noble-print"
$SECRET    = "up-print-refresh-token"
```

---

## 1. Entra app registration

This is what lets the app hold a **delegated** Universal Print token. It cannot
be a managed identity: creating, starting and cancelling a print job are all
documented `Application: Not supported`.

1. **Entra ID → App registrations → New registration**
   - Name: `noble-print`
   - Accounts: *Single tenant*
   - Redirect URI: **leave blank**
   - → **Register**

2. **Authentication → Advanced settings → Allow public client flows → Yes → Save.**
   Without this the device-code sign-in in step 4 cannot start.

3. **API permissions → Add a permission → Microsoft Graph → Delegated**, add:

   | Permission | For |
   |---|---|
   | `Sites.ReadWrite.All` | read the queue, write the four columns |
   | `PrintJob.ReadWriteBasic` | create, start **and cancel** print jobs |
   | `Printer.Read.All` | resolve the printer behind a share |
   | `PrinterShare.ReadBasic.All` | the preflight |
   | `offline_access` | issue a refresh token at all |

   → **Grant admin consent for &lt;tenant&gt;** and confirm every row reads
   *Granted*.

   > Do **not** add `PrintConnector.Read.All`. Only the diagnostic script wants
   > it; the app deliberately does not.

4. Copy from **Overview**:

```powershell
$TENANT_ID = "<Directory (tenant) ID>"
$CLIENT_ID = "<Application (client) ID>"
```

### The service account

Whoever signs in at step 4 **owns every print job**. Use a dedicated account, not
a person's. Its lifecycle is now load-bearing:

- The refresh token lasts **90 days**, rolling.
- It is revoked by a password change, a self-service reset, an admin reset, or an
  explicit revocation. **Password expiry alone does not revoke it.**
- Exclude the account from password-expiry policy, or diarise re-running step 4.

---

## 2. Index the SharePoint column

**Do this before anything else touches SharePoint.** A non-indexed column cannot
be used in a Graph `$filter` *at all* — not "slowly", not "on large lists". Every
query fails.

1. The library → **Settings (gear) → Library settings → More library settings**
2. **Indexed columns → Create a new index**
3. Primary column: **`Print_Status`** → **Create**

While you are there, confirm the four columns exist with these **exact** display
names — `Print_Status`, `Print_JobId`, `Print_Message`, `Printer_Name` — and note
whether **content approval** is on (*Versioning settings*). If it is, tell me:
Graph will not return non-Approved items and the permission set changes.

---

## 3. Azure resources

### 3.1 Resource group and storage

```powershell
az group create --name $RG --location $LOC

az storage account create `
    --name $STORAGE --resource-group $RG --location $LOC `
    --sku Standard_LRS --allow-blob-public-access false
```

### 3.2 Pick a supported Python runtime

**Do not guess this, and do not copy a version from a blog.** Ask the platform:

```powershell
az functionapp list-flexconsumption-runtimes --location $LOC --runtime python `
    --query "[].{version:version, sku:sku.name}" -o table
```

Use a version that command actually returns.

> **Your local venv is Python 3.14.7 and Azure will almost certainly offer
> something lower.** That mismatch is **not a problem**: with remote build the
> platform reinstalls from `requirements.txt` server-side against *its* runtime,
> so no local wheel is shipped. Core Tools may print a version-mismatch warning —
> it is noise. Never change the platform runtime to silence a warning.

```powershell
$PYVER = "3.12"   # <- whatever the command above returned
```

### 3.3 Function App (Flex Consumption)

Flex Consumption, not Consumption: **the Linux Consumption plan is retiring on
30 September 2028 and is receiving no new language versions.**

```powershell
az functionapp create `
    --resource-group $RG --name $APP --storage-account $STORAGE `
    --flexconsumption-location $LOC `
    --runtime python --runtime-version $PYVER `
    --instance-memory 2048
```

### 3.4 Application Insights

```powershell
az monitor log-analytics workspace create `
    --resource-group $RG --workspace-name $WORKSPACE --location $LOC

$WS_ID = az monitor log-analytics workspace show `
    --resource-group $RG --workspace-name $WORKSPACE --query id -o tsv

az monitor app-insights component create `
    --app $INSIGHTS --location $LOC --resource-group $RG `
    --workspace $WS_ID --application-type web

$AI_CONN = az monitor app-insights component show `
    --app $INSIGHTS --resource-group $RG --query connectionString -o tsv
```

### 3.5 Key Vault

```powershell
az keyvault create `
    --name $VAULT --resource-group $RG --location $LOC `
    --enable-rbac-authorization true
```

---

## 4. Identity and RBAC

The single most common cause of "it worked in dev": identical code, missing role.

```powershell
# System-assigned managed identity for the app
az functionapp identity assign --resource-group $RG --name $APP

$PRINCIPAL = az functionapp identity show `
    --resource-group $RG --name $APP --query principalId -o tsv

$VAULT_ID = az keyvault show --name $VAULT --resource-group $RG --query id -o tsv

# Secrets OFFICER, not User: rotation WRITES the new refresh token back.
az role assignment create `
    --assignee-object-id $PRINCIPAL --assignee-principal-type ServicePrincipal `
    --role "Key Vault Secrets Officer" --scope $VAULT_ID
```

> `Key Vault Secrets User` grants **read only**. The app redeems the refresh
> token and Entra returns a *new* one, which must be stored. With read-only
> access rotation fails silently every run and the pipeline stops working after
> ~90 days, with nothing in the code to blame.

Grant yourself the same role so step 5 can write the secret:

```powershell
$ME = az ad signed-in-user show --query id -o tsv
az role assignment create `
    --assignee-object-id $ME --assignee-principal-type User `
    --role "Key Vault Secrets Officer" --scope $VAULT_ID
```

Verify:

```powershell
az role assignment list --scope $VAULT_ID `
    --query "[].{who:principalName, role:roleDefinitionName}" -o table
```

---

## 5. Seed the refresh token

```powershell
cd "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"

$env:GRAPH_TENANT_ID = $TENANT_ID
$env:GRAPH_CLIENT_ID = $CLIENT_ID
$env:KEY_VAULT_URI   = "https://$VAULT.vault.azure.net/"
$env:PRINT_REFRESH_TOKEN_SECRET = $SECRET
Remove-Item Env:\PRINT_REFRESH_TOKEN -ErrorAction SilentlyContinue

.\.venv\Scripts\python.exe scripts\bootstrap_token.py
```

Follow the device-code prompt and **sign in as the print service account**.
Confirm the secret landed:

```powershell
az keyvault secret show --vault-name $VAULT --name $SECRET --query "attributes.created" -o tsv
```

Never print the value, never paste it into a command line or a document.

---

## 6. Application settings

```powershell
az functionapp config appsettings set --resource-group $RG --name $APP --settings `
    "GRAPH_TENANT_ID=$TENANT_ID" `
    "GRAPH_CLIENT_ID=$CLIENT_ID" `
    "KEY_VAULT_URI=https://$VAULT.vault.azure.net/" `
    "PRINT_REFRESH_TOKEN_SECRET=$SECRET" `
    "SHAREPOINT_HOSTNAME=<TENANT>.sharepoint.com" `
    "SHAREPOINT_SITE_PATH=/sites/<SITE>" `
    "PRINT_BATCH_SIZE=5" `
    "PRINT_STATUS_WINDOW_DAYS=20" `
    "PRINT_RESUBMIT_MIN_AGE_HOURS=72" `
    "PRINT_BUSINESS_TZ=America/Vancouver" `
    "PRINT_BUDGET_SECONDS=90" `
    "GRAPH_TIMEOUT_SECONDS=30" `
    "APPLICATIONINSIGHTS_CONNECTION_STRING=$AI_CONN"
```

**`PRINT_REFRESH_TOKEN` must NOT be set here.** It is a local-development escape
hatch. It works in Azure, which is exactly the danger: the app would use a
hardcoded token and never rotate. It logs a warning on every use so the evidence
is in App Insights — but do not create the problem.

Confirm nothing local leaked in:

```powershell
az functionapp config appsettings list --resource-group $RG --name $APP `
    --query "[?name=='PRINT_REFRESH_TOKEN']" -o table    # must be empty
```

---

## 7. Deploy the code

```powershell
.\.venv\Scripts\python.exe -m pytest        # 353 tests must be green FIRST

cd functionapp
func azure functionapp publish $APP --build remote
cd ..
```

Remote build reinstalls from `requirements.txt` on the platform, so no local venv
or wheel is shipped and native packages are built for the target OS.

Publishing runs from `functionapp/`, so everything outside it — `tests/`,
`scripts/`, `.venv/`, `docs/` — is already out of the artifact by construction.
`.funcignore` is what keeps `local.settings.json` out, which is the one that
matters: it holds the local refresh token. Confirm what shipped:

```powershell
az functionapp function list --resource-group $RG --name $APP --query "[].name" -o tsv
# expect: submit_print_jobs, poll_print_status, resubmit_print_jobs
```

### If publish misbehaves

| Symptom | What it actually is | Do |
|---|---|---|
| `Can't find app with name "<app>"` — but it exists | A dropped TLS handshake on the tool's lookup call, which has **no retry**. | **Just retry**, up to ~8 times. Publish is idempotent. This message means "try again", not "wrong name". |
| `Unable to connect to Azure… az CLI` while `az account show` works | The tool shells out to a **child** `az` process; a shell-profile wrapper is invisible to it. `az account show` only reads the local cache and proves nothing. | `az account get-access-token --query expiresOn -o tsv` — the only check that touches the network. |
| A TLS error printed *after* `The deployment was successful!`, non-zero exit | The final invoke-URL fetch failed. The deploy already landed. | **Read the log, not the exit code.** |

---

## 8. Verify — read back, do not assume

**A code default is not the live value.** Read what actually exists:

```powershell
az functionapp config appsettings list --resource-group $RG --name $APP `
    --query "[?starts_with(name,'PRINT_') || starts_with(name,'GRAPH_') || starts_with(name,'SHAREPOINT_') || name=='KEY_VAULT_URI'].{name:name,value:value}" `
    -o table
```

Expected:

| Setting | Value |
|---|---|
| `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` | your registration |
| `KEY_VAULT_URI` | `https://kv-noble-print.vault.azure.net/` |
| `SHAREPOINT_HOSTNAME` / `SHAREPOINT_SITE_PATH` | your site |
| `PRINT_BATCH_SIZE` | `5` |
| `PRINT_STATUS_WINDOW_DAYS` | `20` |
| `PRINT_RESUBMIT_MIN_AGE_HOURS` | `72` |
| `PRINT_BUSINESS_TZ` | `America/Vancouver` |
| `PRINT_REFRESH_TOKEN` | **absent** |

Confirm sampling is off — if adaptive sampling is on, every count in the weekly
report is quietly wrong:

```powershell
az functionapp config appsettings list --resource-group $RG --name $APP `
    --query "[?contains(name,'Sampling')]" -o table
```

Get the host name and a function key:

```powershell
$HOST_NAME = az functionapp show --resource-group $RG --name $APP --query defaultHostName -o tsv
$KEY = az functionapp keys list --resource-group $RG --name $APP --query "functionKeys.default" -o tsv
$BASE = "https://$HOST_NAME"
```

> Flex Consumption apps get a **hashed** default hostname — `<app>.azurewebsites.net`
> may not resolve. Always read `defaultHostName`. A DNS failure there is not an outage.

---

## 9. Smoke test — the deploy is not finished until you read the columns

Same harness as local, just pointed at the deployed app.

```powershell
# 1. Validation path: must 400, must touch nothing.
.\.venv\Scripts\python.exe scripts\test.py badpayload --base-url $BASE --key $KEY

# 2. Dry run: resolves site, library, the four INTERNAL column names, and the
#    printer's real capabilities. Prints nothing on paper.
.\.venv\Scripts\python.exe scripts\test.py dryrun --base-url $BASE --key $KEY `
    --library "Documents" --folder "/Invoices/ToPrint" `
    --printer-share-id "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5"

# 3. Exactly one real file.
.\.venv\Scripts\python.exe scripts\test.py submit --base-url $BASE --key $KEY `
    --library "Documents" --folder "/Invoices/ToPrint" `
    --printer-share-id "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5" --batch-size 1

# 4. Once the page is out, mark it complete.
.\.venv\Scripts\python.exe scripts\test.py status --base-url $BASE --key $KEY `
    --library "Documents" --folder "/Invoices/ToPrint"
```

**5. Open the library and read the four columns back.** This is the step people
skip, and it is the one that catches a silent write failure — the response can
look perfect while the PATCH is failing.

| After step 3 | After step 4 |
|---|---|
| `Print_Status` = `PRINT_PENDING` | `PRINT_COMPLETED` |
| `Printer_Name` = the share id | unchanged |
| `Print_JobId` = a short number (per-printer, starts near 1) | unchanged |
| `Print_Message` = empty | `printed on 2026-08-30 09:14:07` |

**6. Confirm telemetry arrived** — the weekly report depends on it:

```kusto
traces
| where timestamp > ago(30m) and message startswith "PRINT_EVENT"
| project timestamp, message
```

---

## 10. Power Automate flows

Four flows. Each calls the app with the function key in the `code` query
parameter. **No flow may ever write the four columns itself** — one writer only,
or you get a race you will debug at 2 a.m.

| Flow | Recurrence | Body |
|---|---|---|
| **A — Submit** | 15 min | `{"library":"Documents","folder":"/Invoices/ToPrint","printerShareId":"4429bf4e-…","batchSize":5}` |
| **B — Poll** | 10 min | `{"library":"Documents","folder":"/Invoices/ToPrint"}` |
| **C — Resubmit** | daily 02:00 | `{"library":"Documents","folder":"/Invoices/ToPrint","printerShareId":"4429bf4e-…"}` |
| **D — Digest** | Mon 07:00 | SharePoint *Get items* per status; email the counts |

Flow A must loop, because the batch is 5:

```
Recurrence
└─ Do Until   remainingReady = 0   OR   iterations >= 10     ← always bound it
   └─ HTTP POST {BASE}/api/print/submit?code={KEY}
   └─ Parse JSON → remainingReady, submitted, failed, printerAvailable
└─ Condition: failed > 0  or  status <> 200  or  printerAvailable = false  → notify
```

**Do not drop `printerAvailable` from that condition.** When the printer is not
accepting jobs the run is a perfectly healthy `200` with `failed = 0`, so every
other test passes and the flow says nothing. That flag is the only thing in the
response that distinguishes "nothing to print" from "nothing *can* print".

**Poll's cadence is load-bearing.** If Universal Print discards a finished job
before Poll sees it, Poll gets a 404, writes nothing, and Resubmit reprints the
document 72 hours later. Measure how long finished jobs stay readable in your
tenant and set the interval well inside it.

Store the function key in the flow's HTTP action as a **secure input**, never in
a description or a comment.

---

## 11. Monitoring

Build the workbook and alerts from [`design.md` §13](design.md). At minimum,
create the alert for **silence** — a stopped flow produces no errors at all, so
it is the one failure with no other signal:

```kusto
traces
| where timestamp > ago(2h) and message startswith "RUN_SUMMARY"
| summarize lastRun = max(timestamp) by ep = extract("ep=(\\w+)", 1, message)
```

Alert when no `ep=submit` row appears in 2 hours.

---

## 12. Rollback

> **Prerequisite: this project is not under version control yet** — there is no
> `.git` directory, so the first line below has nothing to check out. Run
> `git init` and make an initial commit before you rely on this path; until then
> the only rollback available is the second one.

```powershell
# Re-publish a known-good commit (needs a git repo -- see above)
git checkout <GOOD_SHA>
cd functionapp; func azure functionapp publish $APP --build remote; cd ..

# Or stop the pipeline instantly without touching the app:
#   turn OFF Power Automate flows A and C.
# Files stay PRINT_READY and nothing is lost -- the library IS the queue.
```

Turning the flows off is the safer first move in an incident: it stops new work
immediately and leaves the queue intact.

---

## 13. When something fails

| Symptom | Cause | Fix |
|---|---|---|
| 500, `remedy: run scripts/bootstrap_token.py` | Refresh token revoked or expired | Re-run step 5 |
| 500 naming a column | Display-name mismatch in the library | Fix the column, or change the constants in `print_policy.py` |
| Every query fails | `Print_Status` not indexed | Step 2 |
| 403 from Key Vault in App Insights | Role is *Secrets User*, not *Officer* | Re-run step 4 |
| Works for ~90 days then stops | Rotation was failing all along (read-only vault role) | Step 4, then step 5 |
| `does not accept application/pdf` | Printer capability | Printer-side; `live-printer-check.ps1` shows the real list |
| Stuck `PRINT_PENDING`, nothing prints | Nothing is delivering jobs to the device | [`live-test.md`](live-test.md) stage 1 |
| Counts in the workbook look low | Adaptive sampling got enabled | Step 8 |

Anything not on this list belongs in
[`docs/ai/troubleshooting.md`](ai/troubleshooting.md) once you have confirmed the
cause and verified the fix.

---

## Appendix — one-page checklist

```
[ ]  1  Entra app: public client flows ON, 5 delegated permissions, admin consent
[ ]  2  SharePoint: 4 columns exist, Print_Status INDEXED, approval state known
[ ]  3  Resources: RG, storage, Flex Consumption app, App Insights, Key Vault
[ ]  4  RBAC: app identity = Key Vault Secrets OFFICER (not User)
[ ]  5  bootstrap_token.py run as the SERVICE ACCOUNT; secret exists in the vault
[ ]  6  App settings set; PRINT_REFRESH_TOKEN absent; sampling off
[ ]  7  pytest green, then publish --build remote; 3 functions listed
[ ]  8  Settings read BACK; defaultHostName captured; key captured
[ ]  9  badpayload 400 · dryrun · one real file · READ THE COLUMNS · PRINT_EVENT in AI
[ ] 10  Flows A-D created; A loops on remainingReady with an iteration cap
[ ] 11  Silence alert created
```
