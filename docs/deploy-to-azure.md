# Deploy to Azure — production runbook

Step by step, in order. **The order matters**: several steps depend on the one
before, and two of them fail *silently* if done out of sequence.

> **Do [`docs/e2e-testing.md`](e2e-testing.md) Part A first.** If a page does not
> come out of the tray from a standalone script, nothing here will make it print —
> and Part A needs no Azure resources at all, so it costs nothing to find out.
> This printer accepts `image/pwg-raster` only, so that test is also the one that
> proves the PDF → raster conversion works against the real device.

---

## 0. What a "deploy" actually consists of

Seven independent artifacts. Only #5 is what people mean by "deploy", and
forgetting any of the others produces a working-looking app that does nothing.

| # | Artifact | Deployed by | Skipped ⇒ |
|---|---|---|---|
| 1 | Entra app registration + admin consent | portal / `az ad` | every call 500s on auth |
| 2 | SharePoint column index | SharePoint UI | **every query fails** |
| 3 | Azure resources + RBAC | **Azure portal** | app cannot read its own secret |
| 4 | Refresh token in Key Vault | `bootstrap_token.py` | every call 500s with a remedy message |
| 5 | Function code | `func … publish` | — |
| 6 | Application settings | `az … appsettings set` | app raises on missing config |
| 7 | Power Automate flows | Power Automate | nothing ever runs |

### Prerequisites on your machine

**Read the TLS note below before running any `az` command that touches the
network** — including `az extension add`. Then:

```powershell
az --version          # Azure CLI          -- 2.87.0 is verified sufficient
func --version        # Azure Functions Core Tools v4

az login

# Several subscriptions are visible. Confirm you are in the right one BEFORE
# creating anything -- there is no undo for resources in the wrong place.
az account set --subscription "dev-Document Intelligence"
az account show --query "{name:name, id:id, tenantId:tenantId}" -o table
```

> **No CLI extensions are needed.** Step 3 creates the infrastructure in the
> portal, so `az monitor app-insights` — which lives in the `application-insights`
> extension and is *not* in the CLI core — never gets called. If you later script
> the App Insights parts, install it first or you get
> `ERROR: 'app-insights' is misspelled or not recognized by the system`; upgrading
> the CLI does not help, as that group has never been in core.

> **Do not run `az upgrade` during a deployment.** The CLI will offer it; decline.
> On Windows the upgrade reinstalls the MSI, replacing
> `C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe` — the exact interpreter
> the truststore wrapper below invokes by absolute path — and it downloads that MSI
> through the same inspecting proxy described below. A half-finished upgrade leaves
> you with no working CLI mid-deploy. Upgrade afterwards, deliberately, then
> re-verify with `az group list`.

> ### ⚠️ `az` and the TLS-inspecting proxy — read before the first `az` command
>
> On this machine a plain `az` command that touches the network fails with
> `CERTIFICATE_VERIFY_FAILED`. `az` ships its own Python + OpenSSL, which does not
> trust the inspection agent's root the way .NET tooling does. **`az login`
> succeeding proves nothing** — it authenticates through the browser/broker, which
> uses the Windows trust store; the resource calls afterwards do not.
>
> The fix is installed: an `az` wrapper function in the PowerShell profile pointing
> at `%LOCALAPPDATA%\az-truststore\azrun.py`. So:
>
> * **In an interactive PowerShell window, plain `az` works** — the profile is loaded.
> * **Anywhere else** (a script, a tool that spawns a child process), call it by
>   absolute path. The PATH shim does not win, because machine PATH entries precede
>   user ones:
>   ```powershell
>   & 'C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe' -B "$env:LOCALAPPDATA\az-truststore\azrun.py" <args>
>   ```
>
> Confirm before continuing — this must return the group, not a certificate error:
>
> ```powershell
> az group list --query "[].name" -o tsv
> ```
>
> Expect intermittent `ConnectionResetError 10054` from the same agent, more often
> on heavier responses. **Retry**; it is not a wrong name or a missing resource.
> Full write-up in [`ai/troubleshooting.md`](ai/troubleshooting.md).

### The target subscription — confirmed 2026-08-31

| | |
|---|---|
| Subscription | **dev-Document Intelligence** |
| Subscription ID | `28e61545-cf6b-4c7c-9a6f-e31dfcc050a9` |
| Directory | Noble & Associates Property Management (`noblehomes.ca`) |
| **Tenant ID** | `ce21d3c3-2ce8-4fa8-bb57-69da9b3e7c91` |
| Status | Active, Azure Plan |
| **Your role** | **Owner** |

Two of those are load-bearing:

- **The tenant id is `$TENANT_ID` in step 1.** No need to re-read it from the app
  registration blade — it is the same value, confirmed from three places: the
  portal, `az account show`, and `live-printer-check.ps1`.
- **Owner is what step 4 needs.** Creating role assignments requires *Owner* or
  *User Access Administrator*; *Contributor* cannot do it, and the failure comes
  late, at the Key Vault grant, after the resources already exist.

These are resource identifiers, not credentials — useless without a token. No
secret belongs in this file.

> **This subscription is shared** with the sibling invoice project
> (`rg-content-understanding-dev`, plus storage accounts and app-service plans, all
> in **westus**). Everything here goes in its own resource group, `rg-noble-print`,
> so the two never entangle while sitting in the same region.

### Names used below

**Step 3 creates these in the Azure portal.** Set the variables in PowerShell too
— steps 5 to 9 use them — but nothing here creates anything.

```powershell
$RG        = "rg-noble-print"
$LOC       = "westus"                    # portal shows this as "West US"
$APP       = "func-noble-print"          # must be globally unique
$STORAGE   = "stnobleprint"              # 3-24 lowercase alphanumerics, globally unique
$VAULT     = "kv-noble-print"            # globally unique
$INSIGHTS  = "appi-noble-print"
$WORKSPACE = "log-noble-print"
$SECRET    = "up-print-refresh-token"
```

> **Region: West US**, to sit with the rest of the estate — the sibling invoice
> project's Function App, storage and app-service plans are all in westus. Flex
> Consumption offers Python 3.10–3.14 there (verified 2026-08-31), so nothing is
> given up by co-locating. Every resource below goes in **westus**; a resource in
> the wrong region cannot be moved, only recreated.

---

## 1. Entra app registration

This is what lets the app hold a **delegated** Universal Print token. It cannot
be a managed identity: creating, starting and cancelling a print job are all
documented `Application: Not supported`.

1. **Entra ID → App registrations → New registration**
   - Name: `Noble Universal Print`
   - Accounts: *Single tenant*
   - Redirect URI: **leave blank**
   - → **Register**

2. **Authentication → Allow public client flows → Enabled.**
   On the classic blade this is under *Advanced settings* as a Yes/No radio; on
   **Authentication (Preview)** it is a toggle on the **Settings** tab. Both have
   **Save / Discard** at the foot of the blade — **greyed out means saved**, so if
   Save is still active you have not committed it, and you will not find that out
   until step 5.

   The app is a public client with no secret, so Entra refuses the device-code
   grant outright without this. Leave **Redirect URI** blank: device code flow does
   not use one.

3. **API permissions → Add a permission → Microsoft Graph → Delegated**, add:

   | Permission | For |
   |---|---|
   | `Sites.ReadWrite.All` | read the queue, write the four columns |
   | `PrintJob.ReadWriteBasic` | create, start **and cancel** print jobs |
   | `Printer.Read.All` | resolve the printer behind a share |
   | `PrinterShare.ReadBasic.All` | the preflight |
   | `offline_access` | issue a refresh token at all — **see the note** |

   → **Grant admin consent for &lt;tenant&gt;** and confirm every row reads
   *Granted*.

   > **`offline_access` may not be listed, and that is not a blocker.** It is an
   > OpenID Connect scope, so it lives under the **OpenId permissions** group, not
   > with the Graph resource groups — and some portal builds omit it from the
   > picker entirely. Add it if the search box finds it; otherwise carry on.
   > `graph_auth.SCOPES` deliberately **excludes** it (MSAL injects
   > `offline_access` / `openid` / `profile` itself and rejects them if passed —
   > pinned by `test_the_scopes_exclude_the_reserved_ones`), and it is
   > user-consentable rather than declared. Step 5 is the real check: if no refresh
   > token comes back, `bootstrap_token.py` says so in as many words.

   > Do **not** add `PrintConnector.Read.All`. Only the diagnostic script wants
   > it; the app deliberately does not.

4. Copy the **Application (client) ID** from **Overview**:

```powershell
$TENANT_ID = "ce21d3c3-2ce8-4fa8-bb57-69da9b3e7c91"   # confirmed, step 0
$CLIENT_ID = "bb707828-6e0e-4c2a-9f4d-329aa10c66a5"   # "Noble Universal Print"
```

Neither is a secret — a client id and a tenant id are public identifiers. The
secret is the refresh token, and it never leaves Key Vault.

**Registration completed 2026-08-31**, verified from Overview: supported account
types *My organization only*, state *Activated*, all five delegated permissions
granted tenant-wide, public client flows enabled.

### The service account

Whoever signs in at step 5 **owns every print job**. Use a dedicated account, not
a person's. Its lifecycle is now load-bearing:

- The refresh token lasts **90 days**, rolling.
- It is revoked by a password change, a self-service reset, an admin reset, or an
  explicit revocation. **Password expiry alone does not revoke it.**
- Exclude the account from password-expiry policy, or diarise re-running step 5.

---

## 2. Index the SharePoint column

**Do this before anything else touches SharePoint.** A non-indexed column cannot
be used in a Graph `$filter` *at all* — not "slowly", not "on large lists". Every
query fails.

1. The library → **Settings (gear) → Library settings → More library settings**
2. **Indexed columns → Create a new index**
3. Primary column: **`Print_Status`** → **Create**

While you are there, confirm the four columns exist with these **exact** display
names — `Print_Status`, `Print_JobId`, `Print_Message`, `Printer_Name`.

**Index confirmed 2026-08-31** — `Print_Status`, 1 of a maximum 20 indices, on
list `{6B19AB5B-2823-4073-8B8F-33C3DB52F3E6}` in site
`https://noblehomes.sharepoint.com/sites/PM` ("Noble - Properties"). Those two
values are `SHAREPOINT_HOSTNAME` and `SHAREPOINT_SITE_PATH` in step 6.

**The queue — confirmed 2026-08-31** from the library's own URL, and used verbatim
by every `test.py` command below and by the Power Automate flows in step 10:

| | |
|---|---|
| `--library` | **`AI_DropBox_V2026`** |
| `--folder` | **`/Backup/Invoice`** |

`folder` is matched as a contiguous run of path segments anywhere in the item's
path (`print_policy.folder_matches`), so `/Backup/Invoice` matches
`/sites/PM/AI_DropBox_V2026/Backup/Invoice` and everything beneath it. The site and
library prefix is neither needed nor wanted, and renaming either will not break the
match.

All four columns confirmed present on this library.

> **`NO_PRINT` is in live use as a `Print_Status` value** and appears nowhere in
> this codebase — something upstream writes it. That is harmless, and arguably
> useful: no route queries that value, so those files are inert and can never be
> picked up. Do not "add support" for it. The four statuses this app owns are
> `PRINT_READY`, `PRINT_PENDING`, `PRINT_FAILED`, `PRINT_COMPLETED`.

### Library versioning — checked 2026-08-31, nothing to do

Read from *Library settings → Versioning settings*:

| Setting | Value | Consequence |
|---|---|---|
| **Require content approval** | **No** ✅ | Graph returns every item; no `_ModerationStatus` filtering, no extra permission, no risk of a write hiding an item from the next query |
| Versioning | **major versions only** | Each field write makes a major version. Submit writes twice and Poll once, so expect ~3 versions per printed file |
| Version time limit | **No time limit** | Versions are pruned by count only, never by age |
| Keep major versions | 500 | Ample — a file would have to print ~160 times to reach it |
| Draft Item Security | *(inactive)* | Greyed out because approval and minor versions are both off |
| **Require check out** | **No** ✅ | Load-bearing. If check-out were required, the app's field writes could fail or strand files checked out |

**Both boxes that could have broken this pipeline are off**, so the design needs no
moderation handling. Re-check if anyone changes versioning settings later: turning
content approval on would need a `_ModerationStatus`-aware query and the
*Approve items* permission on the service account.

> **Versions cost storage, not items.** The ~3 versions per printed file do **not**
> count toward the list item count, so they have no bearing on the 5,000 list view
> threshold or the 30M list limit. They do consume site storage quota, and with no
> time limit they are pruned only when a file passes 500 versions — which for a
> document printed a handful of times never happens.

---

## 3. Azure resources — in the portal

Done in the **Azure portal**, not the CLI. Everything goes in **West US**.

> Portal wording drifts between releases. Where a label below does not match what
> you see, match on *meaning* — the values are what matter, not the exact caption.

Throughout: **Subscription** = `dev-Document Intelligence`, **Region** = **West US**.

### 3.1 Resource group

**Portal → Resource groups → + Create**

| Field | Value |
|---|---|
| Subscription | `dev-Document Intelligence` |
| Resource group | `rg-noble-print` |
| Region | **West US** |

→ **Review + create** → **Create**.

A dedicated group is not tidiness: it is what lets you delete the entire
experiment in one action, and it keeps this app from entangling with the sibling
invoice project that shares the subscription.

### 3.2 Storage account

**Portal → Storage accounts → + Create**

| Field | Value |
|---|---|
| Resource group | `rg-noble-print` |
| Storage account name | `stnobleprint` (3–24 lowercase alphanumerics, globally unique) |
| Region | **West US** |
| Performance | Standard |
| Redundancy | **LRS** (Locally-redundant) |

On the **Advanced** tab: **Allow blob public access → Disabled**.

→ **Review + create** → **Create**.

The Function App needs this for its own host state and for the deployment package.
LRS is deliberate — this holds no business data, only runtime bookkeeping.

### 3.3 Which Python version?

The portal only offers versions the platform supports, so the dropdown is the
answer. Verified for **West US**, 2026-08-31: **3.14, 3.13, 3.12, 3.11, 3.10**.

**Choose 3.14.** It matches the local `.venv` (3.14.7), so the offline suite and
the platform run the same minor version. The sibling project has run 3.14 on Flex
Consumption since 2026-08-20.

> Core Tools 4.12.0 prints *"Remote build for Python 3.14 is not yet supported for
> Flex"* during step 7. That warning is **stale and non-blocking** — the sibling
> verified the build succeeds anyway. Do not downgrade the runtime to silence it.

The local/platform version question is moot regardless: remote build reinstalls
from `requirements.txt` server-side against *its* runtime, so no local wheel ships.

### 3.4 Function App — Flex Consumption

**Portal → Function App → + Create → Flex Consumption**

The first screen asks you to pick a hosting option. Choose **Flex Consumption**,
not Consumption: the Linux Consumption plan is retiring on 30 September 2028 and
receives no new language versions.

**Basics**

| Field | Value |
|---|---|
| Resource group | `rg-noble-print` |
| Function App name | `func-noble-print` (globally unique) |
| Region | **West US** |
| Runtime stack | **Python** |
| Version | **3.14** |
| Instance size / memory | **2048 MB** |

**Storage** — select the existing `stnobleprint`.

**Monitoring** — **Enable Application Insights: Yes**, and let it create
`appi-noble-print` (it also creates the Log Analytics workspace).

> Doing App Insights here rather than separately is worth it: the portal wires
> `APPLICATIONINSIGHTS_CONNECTION_STRING` into the app settings for you. Created
> standalone, you have to copy the connection string across by hand in step 6, and
> a missed one produces an app that runs and reports nothing.

**Networking** — defaults. Public access is fine; the endpoints are protected by
the function key.

→ **Review + create** → **Create**. This takes a few minutes.

**Instance memory** is 2048 MB deliberately: rasterizing a PDF page holds a bitmap
in memory, and 512 MB is tight for a multi-page invoice. It can be changed later
under **Settings → Scale and concurrency**.

### 3.5 Key Vault

**Portal → Key Vaults → + Create**

| Field | Value |
|---|---|
| Resource group | `rg-noble-print` |
| Key vault name | `kv-noble-print` (globally unique) |
| Region | **West US** |
| Pricing tier | Standard |
| **Permission model** | **Azure role-based access control (RBAC)** |

→ **Review + create** → **Create**.

> **The permission model matters.** Step 4 grants a *role*; if the vault is left on
> the legacy **access policy** model those role assignments have no effect and the
> app gets 403 at runtime, with nothing in the code to blame.

### 3.6 Confirm what exists

**Portal → Resource groups → `rg-noble-print` → Overview.** Expect five resources,
all **West US**:

| Resource | Type |
|---|---|
| `stnobleprint` | Storage account |
| `func-noble-print` | Function App |
| `appi-noble-print` | Application Insights |
| `log-noble-print` (or an auto-generated name) | Log Analytics workspace |
| `kv-noble-print` | Key vault |

---

## 4. Identity and RBAC — in the portal

The single most common cause of "it worked in dev": identical code, missing role.

### 4.1 Turn on the app's managed identity

**Portal → `func-noble-print` → Settings → Identity → System assigned**

Set **Status → On** → **Save** → **Yes**.

Copy the **Object (principal) ID** that appears. That is the identity you grant
access to next.

### 4.2 Grant the app access to the vault

**Portal → `kv-noble-print` → Access control (IAM) → + Add → Add role assignment**

| | |
|---|---|
| Role | **Key Vault Secrets Officer** |
| Assign access to | **Managed identity** |
| Members | Your Function App, `func-noble-print` |

→ **Review + assign**.

> **Officer, not User.** `Key Vault Secrets User` is **read only**. The app redeems
> the refresh token and Entra returns a *new* one, which must be written back. With
> read-only access, rotation fails silently on every run and the pipeline stops
> working after about 90 days — with nothing in the code to blame and no error
> until it is already broken. This is the single highest-cost mistake in this
> runbook.

### 4.3 Grant yourself the same role

Step 5 writes the secret from your machine, as you.

**Same blade → + Add → Add role assignment**

| | |
|---|---|
| Role | **Key Vault Secrets Officer** |
| Assign access to | **User, group, or service principal** |
| Members | your own account |

→ **Review + assign**.

> Being subscription **Owner** is not enough. Owner grants management-plane rights
> over the vault; reading and writing *secrets* is a data-plane action and needs
> this role explicitly.

### 4.4 Verify

**Portal → `kv-noble-print` → Access control (IAM) → Role assignments.**

Both rows must be present and read **Key Vault Secrets Officer**:

| Who | Role |
|---|---|
| `func-noble-print` (managed identity) | Key Vault Secrets Officer |
| you | Key Vault Secrets Officer |

Role assignments can take a minute or two to take effect. If step 5 fails with a
403, wait and retry before changing anything.

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
    "SHAREPOINT_HOSTNAME=noblehomes.sharepoint.com" `
    "SHAREPOINT_SITE_PATH=/sites/PM" `
    "PRINT_BATCH_SIZE=5" `
    "PRINT_GIVE_UP_DAYS=10" `
    "PRINT_STALL_MINUTES=5" `
    "PRINT_MAX_RETRIES=10" `
    "PRINT_BUSINESS_TZ=America/Vancouver" `
    "PRINT_BUDGET_SECONDS=90" `
    "GRAPH_TIMEOUT_SECONDS=30" `
    "PRINT_RASTER_DPI=300" `
    "PRINT_RASTER_MAX_BYTES=33554432"
```

> **`APPLICATIONINSIGHTS_CONNECTION_STRING` is deliberately absent here** — the
> portal wired it in when you enabled Application Insights during step 3.4. Setting
> it again by hand risks overwriting a correct value with a stale one. Confirm it is
> present in step 8; if it is missing, App Insights was not enabled on the Function
> App and the app will run while reporting nothing.

Prefer the portal? **`func-noble-print` → Settings → Environment variables →
App settings** takes the same names and values one at a time. The CLI form is a
single idempotent command, which is why it is the default here.

`PRINT_RASTER_*` apply only to printers that need rasterizing — see **Printer
profiles** in `README.md`. 300 dpi is what this printer was proven with; a Letter
page lands around 1.4 MB, well inside one upload chunk. Raising it multiplies both
the CPU time per page and the upload, so measure before changing it. An
out-of-range value warns and falls back rather than failing the run.

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
.\.venv\Scripts\python.exe -m pytest        # 487 tests must be green FIRST

# func shells out to the RAW az.cmd for an ARM token and cannot refresh it over
# the network through the inspecting proxy. Pre-warm the cache through the
# truststore wrapper first, or publish fails with "Unable to connect to Azure".
& 'C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe' -B "$env:LOCALAPPDATA\az-truststore\azrun.py" account get-access-token --output none

cd functionapp
func azure functionapp publish $APP --build remote
cd ..
```

Remote build reinstalls from `requirements.txt` on the platform, so no local venv
or wheel is shipped and native packages are resolved for the target OS.

> **The one new dependency worth checking.** `requirements.txt` includes
> `pypdfium2` for the PDF → PWG-raster conversion, **pinned exactly at 5.13.0**.
> It publishes a `py3-none-manylinux_2_17_x86_64` wheel — **verified present for
> 5.13.0** — so it is Python-version agnostic and needs no compiler on the
> platform. Nothing native is installed on the host, which matters because Flex
> Consumption has no custom-container path.
>
> **The pin is load-bearing, and it is the only dependency here that is pinned.**
> Remote build re-resolves `requirements.txt` on the platform, so a range would
> let a later publish of the *same commit* install a newer pdfium than the offline
> suite ever ran against. pdfium may legitimately change its rasterization between
> releases, and the only guard is the byte-exact output hash in
> `tests/test_printing.py` — which is meaningful only while the deployed version
> equals the tested one. To upgrade: bump the pin, run `pytest`, and if the hash
> test fails, print one page (`docs/e2e-testing.md` Part A) and confirm the device
> accepts it before updating `FIXTURE_SHA256_300DPI`.
>
> The `functionapp/printing/` package ships automatically — it is inside the
> published folder, and `.funcignore` excludes only `tests/`, `.venv/`,
> `__pycache__/` and the settings files.

Publishing runs from `functionapp/`, so everything outside it — `tests/`,
`scripts/`, `.venv/`, `docs/` — is already out of the artifact by construction.
`.funcignore` is what keeps `local.settings.json` out, which is the one that
matters: it holds the local refresh token. Confirm what shipped:

```powershell
az functionapp function list --resource-group $RG --name $APP --query "[].name" -o tsv
# expect exactly three:
#   check_printer_health, poll_print_status, submit_print_jobs
```

**Three.** A `resubmit_print_jobs` in that list means an old build is still
deployed — that endpoint was retired on 2026-09-01. Fewer than three means the
worker failed to index the app — usually a missing import or a `requirements.txt`
problem — and the deploy will otherwise look like it succeeded.

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
| `PRINT_GIVE_UP_DAYS` | `10` |
| `PRINT_STALL_MINUTES` | `5` |
| `PRINT_MAX_RETRIES` | `10` |
| `PRINT_BUSINESS_TZ` | `America/Vancouver` |
| `PRINT_RASTER_DPI` | `300` |
| `PRINT_RASTER_MAX_BYTES` | `33554432` |
| `PRINT_REFRESH_TOKEN` | **absent** |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | **present** — set by the portal in 3.4, not by step 6. Missing means App Insights was never enabled on the app, and every count in the weekly report will be empty |

The query above filters by prefix, so widen it to see the App Insights row:

```powershell
az functionapp config appsettings list --resource-group $RG --name $APP `
    --query "[?name=='APPLICATIONINSIGHTS_CONNECTION_STRING'].name" -o tsv
```

Or read the whole set in the portal: **`func-noble-print` → Settings →
Environment variables**.

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

# 2. Dry run: resolves site, library, the four INTERNAL column names, the
#    printer's real capabilities, and WHICH CONVERSION PROFILE would run.
#    Prints nothing on paper.
.\.venv\Scripts\python.exe scripts\test.py dryrun --base-url $BASE --key $KEY `
    --library "AI_DropBox_V2026" --folder "/Backup/Invoice" `
    --printer-share-id "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5"

# 3. Exactly one real file.
.\.venv\Scripts\python.exe scripts\test.py submit --base-url $BASE --key $KEY `
    --library "AI_DropBox_V2026" --folder "/Backup/Invoice" `
    --printer-share-id "4429bf4e-6294-4bcf-bd92-b5f3c3ff47c5" --batch-size 1

# 4. Once the page is out, mark it complete.
.\.venv\Scripts\python.exe scripts\test.py status --base-url $BASE --key $KEY `
    --library "AI_DropBox_V2026" --folder "/Backup/Invoice"
```

In step 2's output, `conversion` must read **`pdf-to-pwg-raster`** for this
printer. `NONE` means no profile matched and every file would fail at preflight.
The `job config` line should match what
[`e2e-testing.md`](e2e-testing.md) Part A printed with.

**5. Open the library and read the four columns back.** This is the step people
skip, and it is the one that catches a silent write failure — the response can
look perfect while the PATCH is failing.

| After step 3 | After step 4 |
|---|---|
| `Print_Status` = `PRINT_PENDING` | `PRINT_COMPLETED` |
| `Printer_Name` = the share id | unchanged |
| `Print_JobId` = a short number (per-printer, starts near 1) | unchanged |
| `Print_Message` = **whatever it held before** (the claim no longer clears it) | `printed on 2026-08-30 09:14:07` — a completion **replaces** the column |

**6. Confirm telemetry arrived** — the weekly report depends on it:

```kusto
traces
| where timestamp > ago(30m) and message startswith "PRINT_EVENT"
| project timestamp, message
```

---

## 10. Power Automate flows

**Three flows.** Each calls the app with the function key in the `code` query
parameter. **No flow may ever write the four columns itself** — one writer only,
or you get a race you will debug at 2 a.m.

| Flow | Recurrence | Body |
|---|---|---|
| **Health** | *(a step inside A and B, not its own flow)* | `{"printerShareId":"4429bf4e-…"}` |
| **A — Submit** | 15 min | `{"library":"AI_DropBox_V2026","folder":"/Backup/Invoice","printerShareId":"4429bf4e-…","batchSize":5}` |
| **B — Poll** | 10 min | `{"library":"AI_DropBox_V2026","folder":"/Backup/Invoice","giveUpDays":10,"stallMinutes":5,"maxRetries":10}` |
| **D — Digest** | Mon 07:00 | SharePoint *Get items* per status; email the counts |

> **There is no Flow C.** A daily Resubmit flow used to exist; recovery moved into
> Flow B on 2026-09-01. If you are working from an older copy of this runbook, do
> not create it — `POST /api/print/resubmit` returns 404. **Health is not its
> replacement**: it is a step inside A and B, reads a printer, and writes nothing.

### Call Health first in both flows

`POST /api/print/health` is a step at the top of each flow, not a flow of its own.
One share read, **no writes**, safe on every tick. The existing `$KEY` authorises
it — `functionKeys.default` is a **host-level** key covering every function, so
there is nothing new to capture.

```
Flow A:  health  →  Condition healthy == false  →  notify, TERMINATE
                 →  else the Do Until / submit loop as before

Flow B:  health  →  Condition healthy == false  →  notify, but CONTINUE
                 →  poll anyway
```

**Flow A gates; Flow B does not.** Poll marks completions, appends retry history
and gives up on rows past `giveUpDays` — none of which needs a working printer.
Skipping Poll during an outage means rows reach no terminal status for exactly as
long as the outage lasts, which is when recovery matters most.

A sick printer is a **200 with `healthy: false`**, never a non-2xx, so the HTTP
action succeeds and the flow keeps control of the branch. Read `healthy`;
`errors[].code` says which of the ten conditions fired.

> **Run B before A** if you ever put them in one flow. Poll's requeue writes
> `PRINT_READY`, which Submit consumes, so Poll-first makes a recovery actionable
> in the same cycle instead of up to 15 minutes later. They are separate flows on
> separate timers here, which is fine — this only matters if you merge them.

### `printFormat` in Flow A's body

Optional, and **the table above deliberately omits it.** Leave it out and Submit
picks a profile from what the printer reports, which is what happened before the
key existed — so an existing flow needs no edit.

| Value | Effect |
|---|---|
| `application/pdf` | Upload the invoice untouched. **No conversion** — it already is a PDF |
| `image/pwg-raster` | Run the PWG converter first, for a printer that takes raster only |
| *(omitted)* | Choose from the printer's `contentTypes` |

> **Do not put `"printFormat":"application/pdf"` in Flow A for the current
> printer.** The Brother MFC-L5800DW reports **`image/pwg-raster` and nothing
> else** (verified against the live API 2026-08-31). Asking it for PDF is a **400
> on every recurrence** — the flow would fail continuously and print nothing.
>
> On this printer the only valid explicit value is `"image/pwg-raster"`, and that
> selects the *same* profile the capabilities already select, so it buys nothing
> but an entry in the run history. **Omitting the key is the right default here.**

Set it when you want the choice to be **yours rather than the device's** — which
only changes an outcome on a printer that reports **both** formats, where the app
would otherwise always pick passthrough. A value the printer does not report is a
**400 at preflight** naming what it does support, and **nothing is claimed**, so a
wrong value cannot strand files — it just means nothing prints until you fix it.

### The three numbers in Flow B's body

They are the whole reason the retry pacing lives in the flow rather than in app
settings: **changing them needs no deploy and no app restart.** Each falls back to
its app setting, then its built-in default, so omitting them is safe.

| Key | Default | What it does |
|---|---|---|
| `stallMinutes` | 5 | How long a print job may sit before it counts as stalled. Also the base of the retry schedule: retry *n* falls due at `stallMinutes × (2ⁿ − 1)` |
| `maxRetries` | 10 | Most requeues one file may get. Stops **new work** at about 3d 13h |
| `giveUpDays` | 10 | When to stop waiting: cancel the outstanding job and write `PRINT_FAILED`. Stops **waiting** |

Between the two bounds is a grace period of roughly 6½ days: no more jobs are
created, but the last one stays live, so a printer that comes back still prints
the document.

An out-of-range value is a **400** — the response names the key and its range.
The response also echoes all three back, so what was actually in force is visible
in the run history rather than inferred from what you meant to send.

### `printerShareId` in Flow B's body — optional, and read this first

Poll accepts it, and when present it is a **hard override**: every job lookup and
every cancel in the run addresses that share instead of the `Printer_Name` on each
row.

**With one printer registered it changes nothing** — the override equals what every
row already says. It also rescues a row whose `Printer_Name` is empty but whose
`Print_JobId` is set (defect G3), which otherwise needs a human.

> **Leave it out unless you want that rescue.** Job ids are per-printer. If the
> override ever names a share a row's job does **not** live on, the lookup and the
> cancel both 404, the 404 reads as "already gone", the row is requeued anyway, and
> the original prints **beside its replacement**. This is defect **F3-R** in
> `docs/ai/open-defects.md`, reopened deliberately.

The guard is in the response: **`printerOverridden` must be 0.** Anything higher is
the number of rows that disagreed with the override, and each one also logs a
WARNING. The warning is graded, so read which you got: a disagreeing row **with**
an outstanding job names **F3** and can genuinely print twice; one **without** a
job says so and is only a configuration mismatch. Add a Flow B condition on it:

```
Condition: printerOverridden > 0  ->  notify
```

If you register a second printer, either drop the key from Flow B or split into one
flow per printer.

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
before Poll sees it, Poll gets a 404, reads that as a stalled attempt, and
requeues the document — printing it twice. Measure how long finished jobs stay
readable in your tenant and keep the interval well inside it. The risk is set by
this cadence, not by the retry schedule, but the reprint now arrives in minutes
rather than three days, so there is less time to catch it by hand.

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

The repository exists (branch `main`), so both paths below are available.

```powershell
# Re-publish a known-good commit
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
| `does not accept application/pdf` | No printer profile matched this device's capability list | The app converts PDF → PWG raster; if it still refuses, the printer reports neither PDF nor `image/pwg-raster`. Check with `test.py dryrun` (`conversion: NONE`) and `live-printer-check.ps1 -DiagnoseOnly` |
| `convert: ...` in `Print_Message` | The document could not be rasterized | A damaged or password-protected PDF, or one over `PRINT_RASTER_MAX_BYTES`. Reproduce locally: `python -m printing <file>.pdf out.pwg --dpi 300` |
| Prints, but the page is cropped or scaled | The raster and the job configuration disagree | `scaling`/`margin`/`dpi` in `printing/profiles.py` must match the render dpi. Graph still reports `completed` — only the paper shows it |
| `ModuleNotFoundError: pypdfium2` | Remote build did not install it | Confirm `pypdfium2` is in `functionapp/requirements.txt` and that publish used `--build remote` |
| Stuck `PRINT_PENDING`, nothing prints | Nothing is delivering jobs to the device | [`live-test.md`](live-test.md) stage 1 |
| Counts in the workbook look low | Adaptive sampling got enabled | Step 8 |

Anything not on this list belongs in
[`docs/ai/troubleshooting.md`](ai/troubleshooting.md) once you have confirmed the
cause and verified the fix.

---

## Appendix — one-page checklist

```
[ ]  0  e2e-testing.md Part A: a page came out of the tray, NOT cropped
[ ]  0  az reaches Azure through the truststore wrapper (az group list works)
[ ]  0  az account show = dev-Document Intelligence (28e61545-...); role = Owner
[ ]  0  did NOT run az upgrade
[ ]  1  Entra app: public client flows ON, 5 delegated permissions, admin consent
[ ]  2  SharePoint: 4 columns exist, Print_Status INDEXED (approval already checked: off)
[ ]  3  PORTAL, all WEST US: RG, storage, Flex Consumption app (Python 3.14,
        2048 MB, App Insights ENABLED in the wizard), Key Vault on the RBAC
        permission model -- 5 resources in the group
[ ]  4  PORTAL: app managed identity ON; app AND you = Key Vault Secrets OFFICER
        (not User) on the vault
[ ]  5  bootstrap_token.py run as the SERVICE ACCOUNT; secret exists in the vault
[ ]  6  App settings set incl. PRINT_RASTER_*; PRINT_REFRESH_TOKEN absent;
        APPLICATIONINSIGHTS_CONNECTION_STRING present (from 3.4); sampling off
[ ]  7  pytest green (487); token pre-warmed; publish --build remote; THREE
        functions listed -- check_printer_health, poll_print_status,
        submit_print_jobs (fewer means an old build)
[ ]  8  Settings read BACK; defaultHostName captured; key captured
[ ]  9  badpayload 400 · dryrun shows conversion=pdf-to-pwg-raster · one real file ·
        READ THE COLUMNS · READ THE PAPER · PRINT_EVENT in AI
[ ]  9  e2e-testing.md Part C: printer off -> status shows requeued=1 and the host
        logs `cancelled` THEN `requeued`; printer on -> exactly ONE sheet
[ ]  9  e2e-testing.md B5a: dryrun --print-format image/pwg-raster echoes
        `requested fmt: image/pwg-raster`; --print-format application/pdf is a
        400 on THIS printer (raster only). Flow A carries NO printFormat
[ ]  9  e2e-testing.md B0a: health is healthy:True; --print-format application/pdf
        is a 200/unhealthy (NOT a 400); image/png IS a 400; printer OFF gives
        PRINTER_NOT_ACCEPTING_JOBS -- and RECORD the observed `state` into
        design.md's status.state row
[ ] 10  Flows A, B, D created -- there is NO Flow C; A loops on remainingReady
        with an iteration cap; B carries stallMinutes/maxRetries/giveUpDays
[ ] 10  BOTH flows call /api/print/health first: A TERMINATES on healthy==false,
        B notifies but POLLS ANYWAY (Poll still marks completions and gives up)
[ ] 10  If Flow B sends printerShareId: printerOverridden == 0 on a real run,
        and a Flow B condition notifies when it is not (F3-R)
[ ] 11  Silence alert created
```
