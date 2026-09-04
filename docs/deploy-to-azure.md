# Deploy to Azure — production runbook

Step by step, in order. **The order matters**: several steps depend on the one
before, and two of them fail *silently* if done out of sequence.

> **Do [`docs/e2e-testing.md`](e2e-testing.md) Part A first.** If a page does not
> come out of the tray from a standalone script, nothing here will make it print —
> and Part A needs no Azure resources at all, so it costs nothing to find out.
> Part A prints through `image/pwg-raster`, so it is also the test that proves the
> PDF → raster conversion works against the real device. **Whether the printer
> *requires* that conversion or merely accepts it decides Flow A's body** — see
> [The printer](#the-printer) below, and settle it before step 10.

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

**Step 3 creates these in the Azure portal**; nothing here creates anything. Set
them in PowerShell as well — `$RG`, `$APP`, `$VAULT`, `$SECRET` and `$SHARE` are
used again in steps 5 to 9. The other four are here so the names are written down
in one place; you type them into the portal rather than into a shell.

```powershell
$RG        = "rg-noble-print"
$LOC       = "westus"                    # portal shows this as "West US"
$APP       = "func-noble-print"          # must be globally unique
$STORAGE   = "stnobleprint"              # 3-24 lowercase alphanumerics, globally unique
$VAULT     = "kv-noble-print"            # globally unique
$INSIGHTS  = "appinsight-noble-print"
$WORKSPACE = "log-analytics-noble-print"
$SECRET    = "up-print-refresh-token"

# The printer share. PERISHABLE -- see "The printer" below before using it.
$SHARE     = "5f488e73-ab80-4a6b-a60a-a0f883e17e2e"
```

> **Region: West US**, to sit with the rest of the estate — the sibling invoice
> project's Function App, storage and app-service plans are all in westus. Flex
> Consumption offers Python 3.10–3.14 there (verified 2026-08-31), so nothing is
> given up by co-locating. Every resource below goes in **westus**; a resource in
> the wrong region cannot be moved, only recreated.

### The printer

| | |
|---|---|
| Printer | **Noble Home MFC** |
| Share id (`$SHARE`) | `5f488e73-ab80-4a6b-a60a-a0f883e17e2e` |
| Content types | **read them — see below** |

The share id is the **perishable** half of the share/printer pair: deleting and
re-creating a share mints a new one while the printer id is untouched, and the
only symptom is a 404. Every recorded copy — this file, `README.md`,
`live-printer-check.ps1`'s default and the Power Automate flow bodies — goes stale
at that moment. Read the current one from *Universal Print → Printers → the
printer → Overview*.

> ### ⚠️ Read this printer's content types before step 10 — they decide Flow A's body
>
> This is not documentation trivia. `printing/PROFILES` is ordered
> `(PwgRasterProfile, PassthroughProfile)`, and `PwgRasterProfile.matches` stands
> aside the moment the printer accepts the source type — rasterizing what the
> device takes natively is wasted work. So **what the printer reports decides what
> an omitted `printFormat` selects**:
>
> | Printer reports | Flow A omits `printFormat` | Flow A sends `image/pwg-raster` |
> |---|---|---|
> | `image/pwg-raster` only | `pdf-to-pwg-raster` | `pdf-to-pwg-raster` — identical |
> | **both** raster **and** `application/pdf` | **`passthrough` — the PDF uploads unconverted** | `pdf-to-pwg-raster` |
>
> Both rows print. But `e2e-testing.md` Part A proves the **raster** path, so on a
> dual-format printer an omitted key puts production on a pipeline nothing bench-
> tested. One command settles it, needs no Function App, and prints nothing:
>
> ```powershell
> .\scripts\live-printer-check.ps1 -DiagnoseOnly
> ```
>
> **Record the answer here with a date**, the way every other verified fact in
> this file is recorded, then follow the matching branch in step 10.
>
> **The repo currently disagrees with itself** and must not be trusted for this:
> `e2e-testing.md:211` says this printer reports both, while its own §B5a callout
> and `README.md` describe the **retired** Brother MFC-L5800DW (share
> `4429bf4e-…`), which reported `image/pwg-raster` and nothing else. Only a live
> read is evidence.

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
   On the classic blade this is under *Advanced settings* as a Yes/No radio with
   **Save / Discard** at the foot — **greyed out means saved**, so if Save is still
   active you have not committed it, and you will not find that out until step 5.
   On **Authentication (Preview)** it is a toggle on the **Settings** tab with
   **no Save button at all** (observed 2026-09-03): the toggle commits itself and
   reads `Enabled` beside it. Trust the toggle's own label there, not the absence
   of a greyed-out Save.

   The app is a public client with no secret, so Entra refuses the device-code
   grant outright without this. Leave **Redirect URI** blank: device code flow does
   not use one.

3. **API permissions → Add a permission → Microsoft Graph → Delegated**, add:

   | Permission | For |
   |---|---|
   | `Sites.ReadWrite.All` | read the queue, write the five columns |
   | `PrintJob.ReadWriteBasic` | create the job, start it **and cancel** it |
   | `PrintJob.Create` | **createUploadSession** — it refuses `ReadWriteBasic`, so without this the pipeline 403s *after* claiming a row (defect L3) |
   | `Printer.Read.All` | resolve the printer behind a share |
   | `PrinterShare.ReadBasic.All` | the preflight |
   | `offline_access` | issue a refresh token at all — **see the note** |

   → **Grant admin consent for &lt;tenant&gt;** and confirm every row reads
   *Granted*.

   > **`offline_access` may not be listed under *Configured permissions*, and that
   > is not a blocker.** It is an OpenID Connect scope, so it lives under the
   > **OpenId permissions** group rather than with the Graph resource groups, and
   > some portal builds omit it from the picker entirely. Add it if the search box
   > finds it; otherwise carry on. `graph_auth.SCOPES` deliberately **excludes** it
   > (MSAL injects `offline_access` / `openid` / `profile` itself and rejects them
   > if passed — pinned by `test_the_scopes_exclude_the_reserved_ones`), and it is
   > user-consentable rather than declared. Step 5 is the real check: if no refresh
   > token comes back, `bootstrap_token.py` says so in as many words.

   > **Expect nine rows, not five, and do not go hunting for the extra four.**
   > Azure adds **`User.Read`** at registration — it is not in `SCOPES`, it is
   > harmless, leave it. And once anyone has signed in, `offline_access`, `openid`
   > and `profile` appear under **Other permissions granted**, consented but not
   > configured. That block is the *desired* state, not a warning: `offline_access`
   > granted is what mints the refresh token. Ignore the portal's suggestion to
   > move them into the configured list — declaring them changes nothing, and MSAL
   > re-requests them at every sign-in regardless.

   > Do **not** add `PrintConnector.Read.All`. Only the diagnostic script wants
   > it; the app deliberately does not.

4. Copy the **Application (client) ID** from **Overview**:

```powershell
$TENANT_ID = "ce21d3c3-2ce8-4fa8-bb57-69da9b3e7c91"   # confirmed, step 0
$CLIENT_ID = "bb707828-6e0e-4c2a-9f4d-329aa10c66a5"   # "Noble Universal Print"
```

Neither is a secret — a client id and a tenant id are public identifiers. The
secret is the refresh token, and it never leaves Key Vault.

**Registration completed 2026-08-31; re-verified 2026-09-03**, each fact read from
the blade that actually shows it. Permissions and public client flows are **not**
visible on Overview, so an earlier version of this line cited a screen that could
not have confirmed them:

| Blade | Confirmed |
|---|---|
| **Overview** | client id `bb707828-…`, tenant `ce21d3c3-…`, *My organization only*, state *Activated*, **no** redirect URI and **no** client secret — both correct for a public client doing device code |
| **API permissions** | all five delegated Graph permissions *Granted*, `Printer.Read.All` being the one whose *Admin consent required* is Yes. Plus the default `User.Read`, and `offline_access` / `openid` / `profile` granted under *Other permissions granted* |
| **Authentication (Preview) → Settings** | **Allow public client flows: Enabled** |

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

While you are there, confirm the **five** columns exist with these exact display
names — `Print_Status`, `Print_JobId`, `Print_Message`, `Printer_Name`, and
`Print_Time`.

> **`Print_Time` must exist BEFORE the code is deployed**, and its *settings*
> matter as much as its existence. Create it as **Date and Time**, with:
>
> | Setting | Value | Why |
> |---|---|---|
> | **Include Time** | **Yes** | The column holds a *moment* — `stallMinutes × (2ⁿ − 1)` lands on minutes. Date-only truncates every retry boundary to midnight and the whole backoff collapses |
> | **Friendly format** | **No** | Renders an absolute timestamp instead of "in 2 days", so the value can be read against the log |
> | Indexed | **No** | The due-time comparison happens in Python: SharePoint honours only one indexed field in a `$filter` and `Print_Status` holds that slot |
>
> Unlike the two silent ordering traps elsewhere in this runbook, a *missing*
> column fails loudly: every Submit and Poll returns 500 naming it until it
> exists. Health keeps working, because it resolves no list. A column that exists
> with the **wrong settings** fails quietly instead — which is why they are a table
> rather than a sentence. Step 9 proves the round trip.

**Index confirmed 2026-08-31** — `Print_Status`, 1 of a maximum 20 indices, on
list `{6B19AB5B-2823-4073-8B8F-33C3DB52F3E6}` in site
`https://noblehomes.sharepoint.com/sites/PM` ("Noble - Properties"). Those two
values go in the **Power Automate flow bodies** as `sharepointHostname` and
`sharepointSitePath` (step 10). They were app settings until 2026-09-02; a leftover
`SHAREPOINT_HOSTNAME` or `SHAREPOINT_SITE_PATH` now does nothing.

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

All five columns confirmed present on this library.

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
| Primary service | **Azure Blob Storage or Azure Data Lake Storage Gen 2** — **required**, despite the blade saying it "doesn't restrict your storage to this resource type". That sentence describes what the choice *does*, not whether you must make one: leave it blank and **Review + create fails** with "Required information is missing or not valid" and a ❌ on Basics. Every option creates the same account; Blob is the honest one, since the deployment package and the host's state and leases are all blobs |
| Performance | Standard |
| Redundancy | **LRS (Locally-redundant)** — ⚠️ **change this.** The blade defaults to **GRS** and pre-ticks the read-access box under it, making it RA-GRS. Selecting LRS removes the checkbox with it |

> Choosing that option does **not** enable hierarchical namespace, despite "Data
> Lake Storage Gen 2" appearing in its name. HNS is the separate **Advanced**
> checkbox in the table below, and it must stay **Disabled**.

The Function App needs this for its own host state and for the deployment package.
LRS is deliberate — this holds no business data, only runtime bookkeeping. The
queue and the audit trail are in SharePoint. Geo-replication would roughly double
the storage rate, permanently, to keep a second copy of host lock blobs and a
deployment package that `func azure functionapp publish` rebuilds in a minute —
and it protects nothing, because if West US is unavailable the Function App is
down with it. There is no failover to take.

### The other tabs — verify, do not change

Every remaining tab is correct on its defaults (observed 2026-09-03). Two are
worth *looking* at, and one of those has moved:

| Tab | Setting | Want | Why it is here |
|---|---|---|---|
| **Security** | *Allow enabling anonymous access on individual containers* | **unchecked** | This is the "Allow blob public access → Disabled" this runbook used to send you to the **Advanced** tab for. It moved to **Security**, and Microsoft has since made unchecked the default — so **verify it, there is nothing to toggle** |
| **Security** | *Enable storage account key access* | **checked** | Leave it on. `AzureWebJobsStorage` connects with a key-based connection string, so turning this off stops the host starting. It reads like the hardening choice and is the one setting on that tab that breaks the deploy |
| **Security** | *Enable Defender for Storage* | **off** | Billed per storage account, and it exists to catch malicious uploads and exfiltration. This account holds host locks and a deployment package; the invoices are in SharePoint and never touch it |
| **Advanced** | *Enable hierarchical namespace* | **unchecked** | Functions does not support ADLS Gen2 / HNS for `AzureWebJobsStorage`, and it **cannot be changed after creation** — getting it wrong means deleting the account and starting again. Access tier stays **Hot**: Cool or Cold add per-transaction charges and early-deletion penalties to state that is read constantly |

Networking, Data protection, Encryption and Tags: defaults, nothing to read.

→ **Review + create** → **Create**. Confirm the summary says `stnobleprint`,
West US, Standard, **LRS**.

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

> **First, create the Log Analytics workspace. The Function App wizard cannot.**
> Its *Create new Application Insights* flyout has a required **Workspace**
> dropdown that lists only workspaces that **already exist**, and offers no way to
> make one — so if you start the wizard without this, the shared
> `DefaultWorkspace-…-WUS` is the only thing you can pick.
>
> **Portal → Log Analytics workspaces → + Create**
>
> | Field | Value |
> |---|---|
> | Subscription | `dev-Document Intelligence` |
> | Resource group | **`rg-noble-print`** |
> | Name | **`log-analytics-noble-print`** |
> | Region | **West US** |
>
> → **Review + create** → **Create**. Takes about a minute.
>
> Already past this and stuck on the default? It is recoverable, not permanent:
> **App Insights → Properties → Change workspace**. Telemetry goes to the shared
> workspace until you do it.

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
| Instance size / memory | **4096 MB** |

**Storage** — select the existing `stnobleprint`.

**Monitoring** — **Enable Application Insights: Yes**, then **override both names
the wizard proposes**:

| Field | The wizard offers | Set it to |
|---|---|---|
| Application Insights | a new resource named after the app, `func-noble-print` | **Create new**, renamed to **`appinsight-noble-print`** |
| Log Analytics workspace *(inside that flyout)* | the shared `DefaultWorkspace-…-WUS` in `DefaultResourceGroup-WUS` — and **nothing else, until you have done the pre-step above** | select **`log-analytics-noble-print`** |

> **The workspace row is the one that gets skimmed past.** Accepting the default
> puts this app's telemetry in a resource group the sibling invoice project also
> uses, which defeats both halves of §3.1's reason for a dedicated group: deleting
> `rg-noble-print` would strand the telemetry rather than take it with it, and the
> two projects entangle in exactly the place §3.1 keeps them apart. §3.6 would also
> then find **four** resources where it expects five. Log Analytics bills on
> ingested data, not on how many workspaces hold it, so a dedicated one costs
> nothing extra.

> Doing App Insights here rather than separately is worth it: the portal wires
> `APPLICATIONINSIGHTS_CONNECTION_STRING` into the app settings for you. Created
> standalone, you have to copy the connection string across by hand in step 6, and
> a missed one produces an app that runs and reports nothing.

**Authentication** — **leave it on secrets.** This tab chooses how three
connections authenticate: host storage (`AzureWebJobsStorage`), deployment
storage, and Application Insights. On the summary they read *"Not applicable when
using secrets"*, which is what you want, and two other steps depend on it:

* §3.2 keeps **Allow storage account key access enabled** *because* the host
  connects with a key-based connection string.
* §4 grants the app's managed identity **only `Key Vault Secrets Officer`** — and
  nothing at all on `stnobleprint`.

So switching these to managed identity here leaves the host with no way to reach
its own storage, and it will not start. The failure is at runtime, not at create
time. Hardening this tab is the natural instinct and it is the wrong move **unless
you also** add three role assignments on `stnobleprint` in §4 — *Storage Blob Data
Owner*, *Storage Queue Data Contributor*, *Storage Table Data Contributor* — and
only then disable key access. The §4.1 managed identity is still required either
way: it is what reaches Key Vault.

**Networking** — defaults. Public access is fine; the endpoints are protected by
the function key.

→ **Review + create** → **Create**. This takes a few minutes.

> **The summary does not show the Log Analytics workspace.** *Monitoring (New)*
> lists only the App Insights name and region, so the workspace you picked cannot
> be confirmed here. Verify it after creation: **`appinsight-noble-print` →
> Properties → Workspace** must read `log-analytics-noble-print`, not
> `DefaultWorkspace-…-WUS`. **Change workspace** on that same blade is the fix.

**Instance memory** is **4096 MB** for headroom: rasterizing a PDF page holds a
bitmap in memory — 512 MB is genuinely tight for a multi-page invoice — and 4 GB
leaves room for higher page counts or a higher `PRINT_RASTER_DPI` later. It is not
free: Flex Consumption bills **memory × execution seconds**, so 4 GB costs twice
2 GB per second of run time. At this volume that is pennies, and it can be changed
either way later under **Settings → Scale and concurrency**.

### 3.5 Key Vault

**Portal → Key Vaults → + Create**

**Basics**

| Field | Value |
|---|---|
| Resource group | `rg-noble-print` |
| Key vault name | `kv-noble-print` (globally unique) |
| Region | **West US** |
| Pricing tier | Standard |
| Soft-delete | **Enabled** — not a choice; it is forced on, 90 days by default |
| Purge protection | **Disable** — see below |

**Access configuration** *(the next tab — the permission model is NOT on Basics)*

| Field | Value |
|---|---|
| **Permission model** | **Azure role-based access control** |
| Resource access — all three | **unchecked**: *Azure VMs for deployment*, *ARM for template deployment*, *Azure Disk Encryption* |

**Networking** — defaults. → **Review + create** → **Create**.

> **The permission model matters, and it is on its own tab.** Follow the Basics
> table to Review + create and you never see the control. Azure RBAC is now the
> **default**, labelled *recommended*, so the risk today is picking *Vault access
> policy* rather than failing to escape it — but the consequence is unchanged:
> step 4 grants a *role*, and on the access-policy model that role has no effect,
> surfacing as a 403 at runtime with nothing in the code to blame.

> **Leave the three Resource access boxes unchecked.** Each one lets an Azure
> platform service read this vault, and this vault holds a live credential. None
> applies: there are no VMs, step 6 sets app settings with `az` rather than an ARM
> template, and no disks are encrypted.

> **Purge protection off, deliberately.** Soft-delete cannot be turned off, so a
> deleted vault **reserves its name** for the retention period. With purge
> protection *off* you can reclaim `kv-noble-print` early by purging manually;
> with it *on* nobody can, including Microsoft, for the full 90 days — which would
> defeat §3.1's reason for a dedicated resource group (delete the whole experiment
> in one action) and §12's rollback. The cost of that choice: after deleting the
> vault, **Key Vaults → Manage deleted vaults → Purge** before recreating, or the
> name is rejected as taken.

### What goes in this vault, and who can read it

**One secret, ever.**

| | |
|---|---|
| Name | `up-print-refresh-token` — `$SECRET`, i.e. `graph_auth.DEFAULT_SECRET_NAME`, overridable by the `PRINT_REFRESH_TOKEN_SECRET` app setting |
| Holds | the delegated OAuth **refresh token** for the print service account |
| Written by | `bootstrap_token.py` at step 5 (as you), then **rewritten on every rotation** by the app — Entra issues a new refresh token on each redemption and `graph_auth.write_refresh_token` stores it back |

That rotation is *why* step 4 grants **Secrets Officer** and not Secrets User:
read-only works for about 90 days, then the token expires unrotated and the
pipeline stops.

**Two principals reach it**, both through `DefaultAzureCredential` — which
resolves to the managed identity in Azure and to your `az login` on your machine:

| Who | Role | Why |
|---|---|---|
| `func-noble-print` managed identity | Key Vault Secrets Officer | reads at runtime, writes the rotated token |
| you | Key Vault Secrets Officer | step 5 writes the secret from your machine |

Anyone holding **Owner** or **User Access Administrator** on the vault, resource
group or subscription can grant themselves that role. Owner alone does not *read*
secrets — that is the management/data-plane split in §4.3 — but it can *assign*
the right to. This is inherent to Azure RBAC; it is stated here so nobody reads
"two principals" as a hard boundary.

> **This secret is not print-only, and that is the part worth knowing.** The token
> carries the service account's delegated scopes (`graph_auth.SCOPES`), and the
> broadest by far is **`Sites.ReadWrite.All`** — read and write **every SharePoint
> site collection that account can reach** — alongside the four print scopes.
> Whoever holds it can act as that account across SharePoint generally. It lasts
> 90 days rolling and is revoked by a password change or reset, **not** by password
> expiry; see the service-account note in §1.

**Two exposures this vault does not cover.** `PRINT_REFRESH_TOKEN` as an app
setting bypasses it completely — a local-development escape hatch that must never
be set in Azure (§6), which is why it logs a warning on every use. And the
**function key** grants no vault access at all, but it lets its holder invoke the
endpoints, make the app use the token, and choose which SharePoint site it acts
on — the trade recorded in CLAUDE.md.

### How the secret stays current — nobody refreshes it by hand

**There are two tokens, and only one of them lives in the vault.**

| | Access token | Refresh token |
|---|---|---|
| Kept in | the worker process's **memory** | **Key Vault** |
| Lasts | ~60–90 minutes | **90 days** |
| Used for | every Graph call | obtaining the next access token |

Every Graph call needs an access token, and `graph_auth.get_access_token` is what
hands one over — it is `GraphClient`'s token provider, called where the
`Authorization` header is built, so it runs **once per HTTP attempt**. One Submit
run makes 15–20 Graph calls, so it is called 15–20 times.

Almost every one of those returns the token already in memory and touches nothing
— no Key Vault, no Entra. Only when the cache is **empty** or the access token is
**within 5 minutes of expiring** does the slow path run:

1. read the refresh token **from the vault**
2. redeem it with Entra, which returns a new access token **and a new refresh
   token**
3. **write that new refresh token back to the vault** ← the rotation
4. cache the access token in memory

So at most **one** rotation per invocation, regardless of how many Graph calls it
makes. The 90-day clock resets every time step 3 runs.

**What decides how often that happens is the worker process, not the call rate.**
The cache is a module-level global, so a fresh Python process starts empty and
rotates on its first Graph call; a process already warm reuses memory for 55–85
minutes. Flex Consumption scales to zero when idle, so at Flow A's 15 minutes and
Flow B's 10 minutes expect a mix of both. Either way the token is rotated many
times a day and **no human ever touches it**.

Rotation is also safe to race: Entra does not revoke an old refresh token when it
is used to fetch a new one, so two overlapping runs both succeed and last-write-
wins leaves a valid token.

> **Two ways this silently stops, both worth knowing before they happen.**
>
> **The write-back fails and says almost nothing.** Step 3 is best-effort by
> design — it logs a warning and carries on, because the access token from step 2
> is valid and failing the run over the *next* token would turn a warning into an
> outage. But if every write fails — which is exactly what **Secrets User instead
> of Secrets Officer** produces (§4.2) — the stored token never advances and the
> pipeline stops dead at 90 days.
>
> **An idle app never rotates.** Rotation only happens when the app runs. §12's
> advice to switch off Flows A and B is the right first move in an incident and is
> safe indefinitely for the *queue* — files simply sit at `PRINT_READY`. It is not
> safe indefinitely for the *token*: past 90 days with no runs, it expires and
> recovery is a manual step 5.

Only that recovery is manual. It cannot be automated — a dead refresh token needs
a human at an interactive sign-in — so every endpoint answers 500 with
`remedy: run scripts/bootstrap_token.py` until someone does. Being revoked by a
password change or reset (not expiry) is the other way to get there; see the
service-account note in §1.

### 3.6 Confirm what exists

**Portal → Resource groups → `rg-noble-print` → Overview.** Expect **six**
resources, all **West US**. Leave *Show hidden types* **unchecked** — enabling
App Insights also creates a *Failure Anomalies* smart-detector alert rule, which
is a hidden type and is not counted here.

| Resource | Type |
|---|---|
| `stnobleprint` | Storage account |
| `func-noble-print` | Function App |
| `ASP-rgnobleprint-…` | App Service plan — **the Flex Consumption plan.** You never create this; the Function App wizard does, and it names it itself, so the four-character suffix differs on any rebuild. It is a resource like any other and it is easy to mistake for something that does not belong |
| `appinsight-noble-print` | Application Insights |
| `log-analytics-noble-print` | Log Analytics workspace — **in this group.** If it is missing, the wizard's default shared workspace was accepted in 3.4; the app still reports, but see the callout there |
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
    --query "length([?name=='PRINT_REFRESH_TOKEN'])" -o tsv    # must print 0
```

> **It counts rather than filters, deliberately.** A filter that matches nothing
> prints nothing — and so does a command that never ran. The local TLS proxy
> resets `az` handshakes often enough (§0) that "no output" is a genuinely
> ambiguous result here. `0` is not ambiguous.

---

## 7. Deploy the code

```powershell
.\.venv\Scripts\python.exe -m pytest        # 570 tests must be green FIRST

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
> `.python_packages/`, `__pycache__/`, `*.py[cod]`, `.vscode/`, `README.md` and
> the two settings files.

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
| **401 or 403 from the SCM endpoint** (not a TLS error) | The app is created with **Basic authentication: Disabled**, which is correct and more secure — Core Tools authenticates to Flex Consumption with an **ARM token**, the one the command above pre-warms. A 401/403 here means it fell back to Kudu instead. | `func-noble-print` → **Settings → Configuration → General settings → SCM Basic Auth Publishing Credentials → On**, retry, then turn it back off. |

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
| `PRINT_REFRESH_TOKEN_SECRET` | `up-print-refresh-token` |
| `PRINT_BUSINESS_TZ` | `America/Vancouver` |
| `PRINT_BUDGET_SECONDS` | `90` |
| `GRAPH_TIMEOUT_SECONDS` | `30` |
| `PRINT_RASTER_DPI` | `300` |
| `PRINT_RASTER_MAX_BYTES` | `33554432` |
| `PRINT_REFRESH_TOKEN` | **absent** |
| `SHAREPOINT_HOSTNAME` / `SHAREPOINT_SITE_PATH` | **absent, or present and inert.** They were app settings until 2026-09-02 and the site is request input now. A leftover does nothing at all — the query above filters for them so you see one rather than wonder |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | **present** — set by the portal in 3.4, not by step 6. Missing means App Insights was never enabled on the app, and every count in the weekly report will be empty |

The query above filters by prefix, so widen it to see the App Insights row:

```powershell
az functionapp config appsettings list --resource-group $RG --name $APP `
    --query "length([?name=='APPLICATIONINSIGHTS_CONNECTION_STRING'])" -o tsv
```

**`1` is the pass; `0` is the failure this check exists to catch.** It counts
instead of printing the name for the same reason as step 6 — a dropped `az`
handshake and a genuinely missing setting used to look identical, and this is the
one where the difference matters.

Or read the whole set in the portal: **`func-noble-print` → Settings →
Environment variables**.

Confirm sampling is off — if adaptive sampling is on, every count in the weekly
report is quietly wrong:

```powershell
az functionapp config appsettings list --resource-group $RG --name $APP `
    --query "length([?contains(name,'Sampling')])" -o tsv
```

> **`0` is the pass.** The authority is `host.json`
> (`samplingSettings.isEnabled: false`), which ships with the code in step 7 — this
> query only looks for an app setting that would *override* it. `0` means nothing
> overrides it. Anything above `0` is the failure.

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
# The site, and the upload format. $FMT must carry whatever Flow A will carry --
# see "The printer" in step 0. Set it to @() only if the printer is raster-only,
# where omitting and naming the format select the SAME profile.
$SITE = @("--hostname", "noblehomes.sharepoint.com", "--site-path", "/sites/PM")
$QUEUE = @("--library", "AI_DropBox_V2026", "--folder", "/Backup/Invoice")
$FMT  = @("--print-format", "image/pwg-raster")

# 1. Validation path: must 400, must touch nothing.
.\.venv\Scripts\python.exe scripts\test.py badpayload --base-url $BASE --key $KEY

# 2. Dry run: resolves site, library, the five INTERNAL column names, the
#    printer's real capabilities, and WHICH CONVERSION PROFILE would run.
#    Prints nothing on paper.
.\.venv\Scripts\python.exe scripts\test.py dryrun --base-url $BASE --key $KEY `
    @SITE @QUEUE --printer-share-id $SHARE @FMT

# 3. Exactly one real file.
.\.venv\Scripts\python.exe scripts\test.py submit --base-url $BASE --key $KEY `
    @SITE @QUEUE --printer-share-id $SHARE @FMT --batch-size 1

# 4. Once the page is out, mark it complete.
.\.venv\Scripts\python.exe scripts\test.py status --base-url $BASE --key $KEY `
    @SITE @QUEUE
```

Read three lines out of the dry run:

* **`content`** — the printer's real capability list. This is the value step 0
  told you to record, and it decides Flow A's body in step 10.
* **`conversion`** — must be **`pdf-to-pwg-raster`** when `$FMT` names raster.
  **`NONE`** means no profile matched and every file would fail at preflight.
  `passthrough` here means `$FMT` was empty *and* the printer accepts PDF — legal,
  but it is not the path Part A proved, so make it a decision rather than a
  surprise.
* **`job config`** — should match what [`e2e-testing.md`](e2e-testing.md) Part A
  printed with.

**5. Open the library and read the five columns back.** This is the step people
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

**7. Prove the `Print_Time` round trip.** The offline suite cannot: `FakeGraph`
stores whatever it is handed, so only the live list can say whether a **Date and
Time** column accepts the offset-bearing ISO string the app writes, returns the
same instant, and is emptied by a JSON `null`. Skip it and a mis-created column
(step 2) surfaces later as a retry schedule that silently does not hold.

```powershell
# Read-only: resolves the column and reports its internal name.
.\.venv\Scripts\python.exe scripts\verify_print_time.py `
    @SITE --library "AI_DropBox_V2026"

# Then, on a PRINT_READY row from that output -- it WRITES to that row and
# restores the original value afterwards, so nominate one you can disturb.
.\.venv\Scripts\python.exe scripts\verify_print_time.py `
    @SITE --library "AI_DropBox_V2026" --item <id>
```

> This one talks to **Graph directly**, not through the deployed app — no
> `--base-url`, no `--key`. It needs the step 5 environment (`GRAPH_TENANT_ID`,
> `GRAPH_CLIENT_ID`, `KEY_VAULT_URI`) in the same shell, or the equivalent in
> `functionapp/local.settings.json`, and it reads the refresh token as **you**,
> which the Secrets Officer role from step 4.3 already allows.

---

## 10. Power Automate flows

**Three flows.** Each calls the app with the function key in the `code` query
parameter. **No flow may ever write the five columns itself** — one writer only,
or you get a race you will debug at 2 a.m.

| Flow | Recurrence | Body |
|---|---|---|
| **Health** | *(a step inside A and B, not its own flow)* | `{"printerShareId":"5f488e73-…"}` |
| **A — Submit** | 15 min | `{"sharepointHostname":"noblehomes.sharepoint.com","sharepointSitePath":"/sites/PM","library":"AI_DropBox_V2026","folder":"/Backup/Invoice","printerShareId":"5f488e73-…","batchSize":5}` |
| **B — Poll** | 10 min | `{"sharepointHostname":"noblehomes.sharepoint.com","sharepointSitePath":"/sites/PM","library":"AI_DropBox_V2026","folder":"/Backup/Invoice","giveUpDays":10,"stallMinutes":5}` |
| **D — Digest** | Mon 07:00 | SharePoint *Get items* per status; email the counts |

> **Flow A's body above omits `printFormat`, and that is only correct on a
> raster-only printer.** Read [`printFormat` in Flow A's body](#printformat-in-flow-as-body)
> below before pasting it. If Health's `content` shows this printer also accepts
> `application/pdf`, add `"printFormat":"image/pwg-raster"` — otherwise Flow A
> silently runs passthrough.

> **The recurrences are not arbitrary, and Flow A's is the expensive one.**
> [`timing.md`](timing.md) is the authority on every clock here. The consequence
> worth knowing before you tune anything: the retry count is derived from the
> file's age when its *first* job is created, so **Flow A every 15 min opens the
> retry ladder at retry 3** — every 5 min at retry 2, every minute at retry 1
> ([`timing.md`](timing.md), "Flow B's cadence no longer eats a boundary"). Flow B
> at 10 min and at 1 min produce identical retry sequences, because `Print_Time`
> pins the instant. Lowering `stallMinutes` compresses the ladder and makes this
> worse, not better.

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

Optional. Leave it out and Submit picks a profile from what the printer reports,
which is what happened before the key existed — so an existing flow needs no edit.

| Value | Effect |
|---|---|
| `application/pdf` | Upload the invoice untouched. **No conversion** — it already is a PDF |
| `image/pwg-raster` | Run the PWG converter first |
| *(omitted)* | Choose from the printer's `contentTypes` |

**Whether to set it is decided by the printer, not by preference.** `PROFILES` is
ordered `(PwgRasterProfile, PassthroughProfile)` and `PwgRasterProfile.matches`
stands aside as soon as the device accepts the source type — rasterizing what the
printer takes natively is wasted work. So an omitted key resolves differently on
the two kinds of device:

| The printer reports | Omit the key | Send `"image/pwg-raster"` |
|---|---|---|
| `image/pwg-raster` only | `pdf-to-pwg-raster` | `pdf-to-pwg-raster` — identical, so omitting is right and the key buys nothing but an entry in the run history |
| **both** raster **and** `application/pdf` | **`passthrough`** — the PDF goes up unconverted | `pdf-to-pwg-raster` |

> **On a dual-format printer, put `"printFormat":"image/pwg-raster"` in Flow A.**
> Both paths print, but only the raster one is bench-proved:
> [`e2e-testing.md`](e2e-testing.md) Part A and Part B run with
> `--print-format image/pwg-raster`. Omitting the key there would put production
> on a pipeline nothing ever tested, and the only visible trace would be
> `conversion: passthrough` in a dry run nobody re-reads.
>
> **On a raster-only printer, omit it** — and never send `"application/pdf"`:
> that is a **400 on every recurrence**, so the flow fails continuously and prints
> nothing.

Which one this printer is, is the read recorded in [The printer](#the-printer).
Step 9's dry run reports it again as `content` and `conversion`.

A value the printer does not report is a **400 at preflight** naming what it does
support, and **nothing is claimed** — so a wrong value cannot strand files; it
just means nothing prints until you fix it. A format this *document* cannot be
turned into is different: that is a per-file `PRINT_FAILED`, one bad file among
good ones.

### The two numbers in Flow B's body

They are the whole reason the retry pacing lives in the flow rather than in app
settings: **changing them needs no deploy and no app restart.** Each falls back to
its built-in default, so omitting them is safe. There is **no app-setting
fallback** any more — the flow body is the only place either can be set.

| Key | Default | What it does |
|---|---|---|
| `stallMinutes` | 5 | How long a print job may sit before it counts as stalled. Also the base of the retry schedule: retry *n* falls due at `stallMinutes × (2ⁿ − 1)` |
| `giveUpDays` | 10 | When to stop: cancel the outstanding job and write `PRINT_FAILED`. **The only bound** |

`maxRetries` was retired on 2026-09-02, along with the grace period. It bounded the
*work* while `giveUpDays` bounded the *waiting*, but the retry count is derived from
the file's age anyway, so the two were one quantity in different units. A flow still
sending `maxRetries` is accepted and ignored — which is what lets the code deploy
before the flows are edited.

An out-of-range value is a **400** — the response names the key and its range.
The response echoes `giveUpDays`, `stallMinutes` and `printerShareId` back, so
what was actually in force is visible in the run history rather than inferred from
what you meant to send.

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
#   turn OFF Power Automate flows A and B.
# Files stay PRINT_READY and nothing is lost -- the library IS the queue.
```

Turning the flows off is the safer first move in an incident: it stops new work
immediately and leaves the queue intact.

> **Safe indefinitely for the queue, not for the token.** Files sit at
> `PRINT_READY` for as long as you like — the library is the queue and nothing
> ages out. But the refresh token is only rotated *when the app runs* (§3.5), so
> flows left off for more than **90 days** let it expire, and the first run after
> that 500s until someone re-runs step 5. Anything shorter costs nothing.

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
| Stuck `PRINT_PENDING`, nothing prints | Nothing is delivering jobs to the device | [`e2e-testing.md`](e2e-testing.md) Part A |
| Prints, but not the pipeline Part A proved | Flow A omitted `printFormat` on a printer that also accepts PDF, so passthrough won | Step 10, [`printFormat` in Flow A's body](#printformat-in-flow-as-body) |
| Counts in the workbook look low | Adaptive sampling got enabled | Step 8 |
| `ConnectionResetError [WinError 10054]` or `Connection aborted` from any `az` command | Local TLS interception on the **workstation**, not Azure. Heavier responses hit it more often | **Retry.** The `az` wrapper in the PowerShell profile already retries five times and is *silent on success*, so `az attempt N failed … retrying…` followed by nothing — and no `az failed after 5 attempts` warning — means a later attempt worked. §0, [`ai/troubleshooting.md`](ai/troubleshooting.md) |

Anything not on this list belongs in
[`docs/ai/troubleshooting.md`](ai/troubleshooting.md) once you have confirmed the
cause and verified the fix.

---

## Appendix — one-page checklist

```
[ ]  0  e2e-testing.md Part A: a page came out of the tray, NOT cropped
[ ]  0  PRINTER content types READ and RECORDED with a date (live-printer-check
        -DiagnoseOnly). Raster-only or dual-format? It decides Flow A's body
[ ]  0  az reaches Azure through the truststore wrapper (az group list works)
[ ]  0  az account show = dev-Document Intelligence (28e61545-...); role = Owner
[ ]  0  did NOT run az upgrade
[ ]  1  Entra app: public client flows ON, 5 delegated permissions, admin consent
[ ]  2  SharePoint: 5 columns exist, Print_Status INDEXED (approval already checked: off)
[ ]  2  Print_Time is Date and Time with INCLUDE TIME on, friendly format off,
        NOT indexed -- wrong settings fail quietly, a missing column does not
[ ]  3  PORTAL, all WEST US: RG, storage, Log Analytics workspace FIRST, Flex
        Consumption app (Python 3.14, 4096 MB, App Insights ENABLED in the wizard,
        Authentication left on SECRETS), Key Vault on the RBAC permission model
        -- SIX resources in the group: the App Service plan (ASP-rgnobleprint-...)
        is created by the wizard and counts too
[ ]  3  App Insights -> Properties -> Workspace reads log-analytics-noble-print,
        NOT DefaultWorkspace-... (the create summary never shows this)
[ ]  4  PORTAL: app managed identity ON; app AND you = Key Vault Secrets OFFICER
        (not User) on the vault
[ ]  5  bootstrap_token.py run as the SERVICE ACCOUNT; secret exists in the vault
[ ]  6  App settings set incl. PRINT_RASTER_*; PRINT_REFRESH_TOKEN absent;
        APPLICATIONINSIGHTS_CONNECTION_STRING present (from 3.4); sampling off
[ ]  7  pytest green (570); token pre-warmed; publish --build remote; THREE
        functions listed -- check_printer_health, poll_print_status,
        submit_print_jobs (fewer means an old build)
[ ]  8  Settings read BACK; defaultHostName captured; key captured; sampling
        query returned 0 (host.json is the authority); App Insights count = 1
[ ]  9  badpayload 400 · dryrun shows conversion=pdf-to-pwg-raster · one real file ·
        READ THE COLUMNS · READ THE PAPER · PRINT_EVENT in AI
[ ]  9  verify_print_time.py: column resolves, a written value returns the SAME
        INSTANT, null clears it. FakeGraph cannot prove this -- only the live list
[ ]  9  e2e-testing.md Part C: printer off -> status shows requeued=1 and the host
        logs `cancelled` THEN `requeued`; printer on -> exactly ONE sheet
[ ]  9  e2e-testing.md B5a: dryrun --print-format image/pwg-raster echoes
        `requested fmt: image/pwg-raster`. On a RASTER-ONLY printer
        --print-format application/pdf is a 400; on a DUAL-FORMAT one it is
        accepted and selects passthrough -- which is the whole reason to decide
[ ]  9  e2e-testing.md B0a: health is healthy:True; a format the PRINTER does not
        report is a 200/unhealthy FORMAT_NOT_SUPPORTED, never a 400 (so on a
        raster-only printer that is application/pdf; on a dual-format one that
        format is simply healthy); an UNKNOWN format like image/png IS a 400;
        printer OFF gives PRINTER_NOT_ACCEPTING_JOBS -- and RECORD the observed
        `state` into design.md's status.state row
[ ] 10  Flows A, B, D created -- there is NO Flow C; A loops on remainingReady
        with an iteration cap; B carries stallMinutes/giveUpDays (NOT maxRetries)
[ ] 10  Flow A's printFormat matches the step-0 reading: OMITTED on a raster-only
        printer, "image/pwg-raster" on one that also accepts PDF (else Flow A
        runs passthrough, which Part A never proved)
[ ] 10  Every flow body carries share 5f488e73-... -- NOT the retired 4429bf4e-...
[ ] 10  BOTH flow bodies carry sharepointHostname + sharepointSitePath -- without
        them Submit and Poll are 400. Health is the signal during the cutover
[ ] 10  BOTH flows call /api/print/health first: A TERMINATES on healthy==false,
        B notifies but POLLS ANYWAY (Poll still marks completions and gives up)
[ ] 10  If Flow B sends printerShareId: printerOverridden == 0 on a real run,
        and a Flow B condition notifies when it is not (F3-R)
[ ] 11  Silence alert created
```
