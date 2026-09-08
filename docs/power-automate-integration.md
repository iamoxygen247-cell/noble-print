# Power Automate integration — the twelve questions

Everything a flow author needs to build or edit the flows that drive this
Function App, answered from the repo rather than from convention.

**How to read this file.** Every factual claim carries a `file:line` citation, so
each answer can be checked at its source. Where the repo does **not** decide
something, this file says so in those words — marked
**`NOT DECIDED IN THIS REPO`** — and stops there rather than inventing an
answer. The appendix collects those gaps, and separately collects the three
places where the repo contradicts *itself*.

Line numbers were re-checked against the working tree on 2026-09-07, after R24
moved `print_policy.py` and `function_app.py`. They rot; the quoted text does not.

---

## 1. What should trigger the flow: Power Apps, SharePoint, email, manual execution, recurrence, or another event?

**Recurrence — and it is the only trigger the repo ever describes.**

Both flows are built on a Power Automate *Recurrence* trigger. `design.md:151`
opens Flow A's sketch with the literal word `Recurrence`, and `design.md:172`
does the same for Flow B. The schedule belongs to the flow, not to the app:

> **No timer trigger exists anywhere in the function app** — both routes are
> HTTP-triggered. Every row here is a flow edit with nothing to redeploy.
> — `timing.md:171-172`

The ownership table makes it a rule rather than an accident
(`design.md:127-128`): *"**Power Automate decides when and how often. The
Function App decides what happens.** Any drift across this line is a bug."*, with
`Schedule / recurrence` listed as Power Automate's (`design.md:132`).

| Flow | Recurrence | Source |
|---|---|---|
| **A — Print Submit** | every **15 min** | `design.md:151`, `timing.md` §6 |
| **B — Print Status Poll** | every **10 min** | `design.md:170`, `timing.md` §6 |
| **D — Weekly digest** | **Mondays 07:00** | `design.md:189`, `:1303` |

### Why polling rather than an event

The reasons that *are* recorded:

- **The library is the queue.** *"An upstream process drops files in with
  `Print_Status = "PRINT_READY"`. They must reach a physical printer through
  Microsoft Universal Print, with the outcome written back onto each file so
  **the library is both the work queue and the audit trail** — no separate
  database."* (`design.md:15-17`)
- **Nothing in scope sets `PRINT_READY`.** *"**G4 — Nothing sets `PRINT_READY`.**
  Out of scope by design; an upstream process owns it."* (`design.md:120`)
- **The model is batch-and-drain, not per-item.** *"Small synchronous batch
  (default **5**, max 15); the flow loops on `remainingReady`"* (`design.md:47`).
- **Poll's cadence is a correctness control**, not a preference — a job purged
  before Poll sees it is reprinted (`timing.md:238-244`; UC-9 at `design.md:902`).

### What is not written down

**`NOT DECIDED IN THIS REPO`** — a SharePoint *"When an item is created"*
trigger, a Power Apps trigger, a manual/button trigger and a mail-arrival trigger
are never mentioned or evaluated anywhere in `docs/`, so there is no recorded
rationale for preferring recurrence over them beyond the queue model above.

---

## 2. Should Submit, Status, and Health live in one flow, separate child flows, or a combination?

**Separate flows on separate timers. Health is a step inside each of them, never
a flow of its own.**

> **Three flows.** Each calls the app with the function key in the `code` query
> parameter. **No flow may ever write the five columns itself** — one writer
> only, or you get a race you will debug at 2 a.m.
> — `deploy-to-azure.md:1084-1086`

| Flow | Recurrence | Calls |
|---|---|---|
| **Health** | *(a step inside A and B, not its own flow)* — `deploy-to-azure.md:1090` | `/api/print/health` |
| **A — Submit** | 15 min | `/api/print/health`, then `/api/print/submit` in a `Do Until` |
| **B — Poll** | 10 min | `/api/print/health`, then `/api/print/status` |
| **D — Digest** | Mon 07:00 | SharePoint *Get items* per status; no function code |

On Health's placement:

> `POST /api/print/health` is a step at the top of each flow, not a flow of its
> own. One share read, **no writes**, safe on every tick. The existing `$KEY`
> authorises it — `functionKeys.default` is a **host-level** key covering every
> function, so there is nothing new to capture.
> — `deploy-to-azure.md:1116-1120`

### Child flows

**`NOT DECIDED IN THIS REPO`** — the term "child flow" appears nowhere in the
repository. There is no position for or against.

### If you ever merge them

> **Run B before A** if you ever put them in one flow. Poll's requeue writes
> `PRINT_READY`, which Submit consumes, so Poll-first makes a recovery
> actionable in the same cycle instead of up to 15 minutes later. They are
> separate flows on separate timers here, which is fine — this only matters if
> you merge them.
> — `deploy-to-azure.md:1140-1143`

### The rule no flow may break

> **Single-writer rule (§P-2.4).** The flow must **never** patch the five columns
> itself, even for a quick fix. Two writers to one field is a race debugged at
> 2 a.m. If the flow needs a state changed, it calls an endpoint.
> — `design.md:145-147`

`Writing any of the five columns` is the Function App's, *exclusively*
(`design.md:135`).

### There is no Flow C

> **There is no Flow C.** A daily Resubmit flow used to exist; recovery moved
> into Flow B on 2026-09-01. If you are working from an older copy of this
> runbook, do not create it — `POST /api/print/resubmit` returns 404. **Health is
> not its replacement**: it is a step inside A and B, reads a printer, and writes
> nothing.
> — `deploy-to-azure.md:1111-1114`

---

## 3. What are the full endpoint URLs and HTTP methods for each environment?

**Three routes, all `POST`, in one deployed environment.**

| Endpoint | Method | Route | Handler |
|---|---|---|---|
| Health | `POST` | `/api/print/health` | `check_printer_health` — `function_app.py:1245` |
| Submit | `POST` | `/api/print/submit` | `submit_print_jobs` — `function_app.py:691` |
| Poll | `POST` | `/api/print/status` | `poll_print_status` — `function_app.py:864` |

The route named "Poll" is `print/status`: "Poll" is its role, `print/status` is
its path. `POST /api/print/resubmit` **returns 404** and must not be called
(`deploy-to-azure.md:1113`).

### Environments

There is **one** deployed environment. No dev/test/staging split exists anywhere
in the repo — one resource group, one Function App, one vault, one site.

| | Value | Source |
|---|---|---|
| Subscription | `dev-Document Intelligence` (`28e61545-cf6b-4c7c-9a6f-e31dfcc050a9`) | `deploy-to-azure.md:92-102` |
| Tenant | `ce21d3c3-2ce8-4fa8-bb57-69da9b3e7c91` (noblehomes.ca) | `deploy-to-azure.md:98` |
| Resource group | `rg-noble-print` | `deploy-to-azure.md:128` |
| Region | `westus` | `deploy-to-azure.md:129` |
| Function App | `func-noble-print` | `deploy-to-azure.md:130` |
| Key Vault | `kv-noble-print` | `deploy-to-azure.md:132` |
| App Insights | `appinsight-noble-print` | `deploy-to-azure.md:133` |

"dev" in the subscription name is the *subscription's* name — that subscription
hosts this and the sibling invoice project. It is not a second environment.

### The base URL — read it, do not assume it

The literal hostname is recorded nowhere in the repo, deliberately:

> Flex Consumption apps get a **hashed** default hostname —
> `<app>.azurewebsites.net` may not resolve. Always read `defaultHostName`. A DNS
> failure there is not an outage.
> — `deploy-to-azure.md:991-992`

So `{BASE}` is derived, not typed (`e2e-testing.md:596-599`):

```powershell
$HOST_NAME = az functionapp show --resource-group rg-noble-print `
    --name func-noble-print --query "defaultHostName" -o tsv
$BASE = "https://$HOST_NAME"
```

Portal equivalent: **`func-noble-print` → Overview → Default domain**.

| Target | Base URL |
|---|---|
| Deployed | `https://{defaultHostName}` — from the command above |
| Local (`func start`) | `http://localhost:7071` — `e2e-testing.md:15` |

Full form as the flows use it: `{BASE}/api/print/submit?code={KEY}`
(`deploy-to-azure.md:1244`).

### The function key stays a placeholder

Write it as `YOUR_FUNCTION_KEY` in any document. Read the real value with:

```powershell
$KEY = az functionapp keys list --resource-group rg-noble-print `
    --name func-noble-print --query "functionKeys.default" -o tsv
```

> Store the function key in the flow's HTTP action as a **secure input**, never
> in a description or a comment.
> — `deploy-to-azure.md:1261-1262`

---

## 4. What authentication does the Function App require: function key, Entra ID, managed identity, API Management, or another method?

**Inbound: a function key, and nothing else.** There are three separate trust
boundaries here, and conflating them is the usual mistake.

### Inbound — caller to Function App

All three routes are declared `auth_level=func.AuthLevel.FUNCTION`
(`function_app.py:691`, `:864`, `:1234`). That means:

- **Function key only.** Present it as `?code=<key>` or an `x-functions-key`
  header.
- **No** App Service Authentication / Easy Auth, **no** Entra ID token
  validation, **no** API Management, **no** per-caller identity. The app never
  learns who called it.
- `functionKeys.default` is a **host-level** key covering every function, so
  Health needs no key of its own (`deploy-to-azure.md:1118-1120`).

**The consequence to understand before handing the key out.**
`sharepointHostname` and `sharepointSitePath` moved out of app settings and into
the request body on 2026-09-02. Per `CLAUDE.md`, that means one Function App
serves any site a flow names — and **whoever holds the function key chooses the
site**, bounded only by what the delegated service account can reach. The
widening was deliberate.

### Outbound — Function App to Graph and Universal Print

**A delegated user token, not managed identity, and not by choice.**

> **WHY THIS IS NOT MANAGED IDENTITY.** Universal Print's job APIs do not accept
> app-only tokens. The documentation is explicit -- "Application: Not supported"
> -- for creating a job (both the printerShare and printer routes), starting one,
> and cancelling one, and createUploadSession on a share is "supported with
> delegated permissions only". A Function App has no signed-in user, so the only
> way to hold a delegated token unattended is to acquire one interactively ONCE
> and keep redeeming its refresh token.
> — `graph_auth.py:3-10`

The shape: a human runs `scripts/bootstrap_token.py` once for a device-code
sign-in as the print service account → the refresh token is stored in Key Vault
as `up-print-refresh-token` → this module redeems it for access tokens and
writes the rotated refresh token back. It is a `msal.PublicClientApplication`
(`graph_auth.py:186`), so there is **no client secret** anywhere.

Delegated scopes (`graph_auth.py:68-74`), all five required:
`Sites.ReadWrite.All`, `PrintJob.ReadWriteBasic`, `PrintJob.Create`,
`Printer.Read.All`, `PrinterShare.ReadBasic.All`.

### Outbound — Function App to Key Vault

**Managed identity — the only place it is used.**
`SecretClient(vault_url=key_vault_uri(), credential=DefaultAzureCredential())`
(`graph_auth.py:126`), with the app's system-assigned identity holding
**Key Vault Secrets Officer** (`deploy-to-azure.md:619`) because it both reads
and writes the rotated token.

### Two calls that must carry no Authorization header

Per `CLAUDE.md` rule 3, and asserted by tests: the SharePoint pre-authenticated
download URL, and the Universal Print upload-session `PUT` — Graph documents that
adding a bearer token there *"might result in an HTTP 401"*. Both go through
`graph_client`'s separate unauthenticated session.

### Operational note

The refresh token lasts **90 days of inactivity** and rotates on every use, so a
regularly-running app never ages out. What kills it: a password change, MFA
reset, or admin revocation — *"**Password expiry alone does not.**"* When it
dies, every endpoint 500s with an unambiguous instruction to re-run the bootstrap
script. And:

> **Nothing alerts on this** — design §13.8 watches for *silence in the logs*,
> which reads identically to a quiet week.
> — `timing.md:235-236`

---

## 5. Could you provide example request and response payloads for Submit, Status, and Health?

Taken from the route bodies, not from prose. All message strings below are the
literal output of their builders in `print_policy.py`.

Two request keys behave unusually and are a common source of 400s: **`folder`
and `sharepointSitePath` must be *present*, but `""` is a legal value** — `""`
means the library root / the root site, so an absent key cannot be read as a
default (`function_app.py:176-183`, `:236-253`).

### 5.1 Submit — `POST {BASE}/api/print/submit?code=YOUR_FUNCTION_KEY`

**Request** (`function_app.py:697-709`)

```json
{
  "library": "AI_DropBox_V2026",
  "folder": "/Backup/Invoice",
  "printerShareId": "5f488e73-ab80-4a6b-a60a-a0f883e17e2e",
  "sharepointHostname": "noblehomes.sharepoint.com",
  "sharepointSitePath": "/sites/PM",
  "batchSize": 5,
  "printFormat": "image/pwg-raster",
  "dryRun": false
}
```

| Key | Required | Default | Notes |
|---|---|---|---|
| `library` | yes | — | non-empty string |
| `folder` | key required | — | `""` or `"/"` = library root |
| `printerShareId` | yes | — | non-empty string |
| `sharepointHostname` | yes | — | non-empty string |
| `sharepointSitePath` | key required | — | `""` or `"/"` = root site |
| `batchSize` | no | `5` | range 1–15; out of range is a **400** |
| `printFormat` | no | *capabilities decide* | `application/pdf` or `image/pwg-raster`; unknown value is a **400** |
| `dryRun` | no | falsy | any truthy value switches to the dry-run branch |

**200 — normal run** (`function_app.py:833-861`)

```json
{
  "library": "AI_DropBox_V2026",
  "folder": "/Backup/Invoice",
  "printerShareId": "5f488e73-ab80-4a6b-a60a-a0f883e17e2e",
  "printFormat": null,
  "batchSize": 5,
  "candidatesFound": 3,
  "remainingReady": 0,
  "submitted": 1,
  "failed": 1,
  "skipped": 1,
  "notYetDue": 4,
  "budgetExhausted": false,
  "printerAvailable": true,
  "items": [
    { "itemId": "42", "fileName": "INV-1001.pdf",
      "result": "submitted", "jobId": "1187" },
    { "itemId": "43", "fileName": "INV-1002.pdf",
      "result": "failed",
      "message": "printer does not accept application/pdf (supports: image/pwg-raster)" },
    { "itemId": "44", "fileName": "INV-1003.pdf",
      "result": "skipped",
      "message": "another run claimed this file first" }
  ]
}
```

- `printFormat: null` means the printer's capabilities chose.
- `remainingReady` counts **only due files**. Flow A's `Do Until` tests it, so
  `notYetDue` is deliberately excluded — *"a file waiting on a future Print_Time
  can never be submitted this cycle, so counting it there would spin the loop to
  its iteration cap every recurrence"* (`design.md:518-523`).
- A `submitted` item may additionally carry a **`warning`** key. That is
  `CLAUDE.md` rule 1's unrecorded-job-id case: the paper is on its way but
  `Print_JobId` could not be written, so a duplicate is coming
  (`function_app.py:682-684`).

**200 — printer not accepting jobs** — a *different shape*: no `batchSize` key,
and `remainingReady` is forced to `0` so Flow A's loop can terminate
(`function_app.py:739-758`)

```json
{
  "library": "AI_DropBox_V2026",
  "folder": "/Backup/Invoice",
  "printerShareId": "5f488e73-ab80-4a6b-a60a-a0f883e17e2e",
  "printFormat": null,
  "candidatesFound": 0,
  "remainingReady": 0,
  "submitted": 0,
  "failed": 0,
  "skipped": 0,
  "notYetDue": 0,
  "budgetExhausted": false,
  "printerAvailable": false,
  "items": [],
  "message": "printer share Noble Home MFC is not accepting jobs (state: stopped)"
}
```

**200 — `"dryRun": true`** — nothing claimed, nothing written
(`function_app.py:784-812`)

```json
{
  "dryRun": true,
  "library": "AI_DropBox_V2026",
  "folder": "/Backup/Invoice",
  "resolvedColumns": {
    "Print_Status": "Print_x005f_Status",
    "Print_JobId": "Print_x005f_JobId",
    "Print_Message": "Print_x005f_Message",
    "Printer_Name": "Printer_x005f_Name",
    "Print_Time": "Print_x005f_Time"
  },
  "printer": {
    "shareId": "5f488e73-ab80-4a6b-a60a-a0f883e17e2e",
    "printerId": "cf8d9fa1-0502-4b0d-b28e-22a52cdde8a1",
    "displayName": "Noble Home MFC",
    "acceptingJobs": true,
    "state": "idle",
    "contentTypes": ["image/pwg-raster"],
    "dpis": [300]
  },
  "printFormat": null,
  "conversion": {
    "supported": true,
    "profile": "pdf-to-pwg-raster",
    "sourceContentType": "application/pdf",
    "uploadContentType": "image/pwg-raster",
    "requestedFormat": null,
    "conversionRequired": true,
    "jobConfiguration": {
      "copies": 1,
      "dpi": 300,
      "orientation": "portrait",
      "duplexMode": "oneSided",
      "colorMode": "grayscale",
      "inputBin": "auto",
      "outputBin": "face-down",
      "mediaSize": "North America Letter",
      "mediaType": "stationery",
      "quality": "medium",
      "scaling": "fit",
      "margin": { "top": 4320, "bottom": 4320, "left": 4320, "right": 4320 }
    }
  },
  "candidatesFound": 2,
  "notYetDue": 1,
  "wouldSubmit": [
    { "itemId": "42", "fileName": "INV-1001.pdf",
      "created": "2026-09-04 08:31:07+00:00", "printTime": null }
  ]
}
```

Notes on that block, because two values are easy to get wrong:

- **`margin` is in microns**, not points or millimetres. `4320` is
  `FALLBACK_MARGIN_MICRONS`, used when the printer reports no margins
  (`printing/profiles.py:65`, `:199-210`). A printer that reports its own margins
  yields its values instead.
- `mediaSize` and `scaling` fall back to `"North America Letter"` and `"fit"`
  only when the printer reports nothing (`printing/profiles.py:213-216`).
  `scaling: fit` is load-bearing, not cosmetic.
- `resolvedColumns` shows whatever SharePoint actually assigned. The encoded form
  is the expected case but is *"Plausible, unverified for your tenant"*
  (`design.md:238-241`) — never hardcode it.
- Datetimes are serialised with `json.dumps(..., default=str)`, hence the
  space-separated `2026-09-04 08:31:07+00:00` form rather than ISO `T`.
- `jobConfiguration` for the **passthrough** profile is just `{"copies": 1}`
  (`print_policy.py:107`) — the printer's own defaults decide the rest.

### 5.2 Poll — `POST {BASE}/api/print/status?code=YOUR_FUNCTION_KEY`

**Request** (`function_app.py:870-900`)

```json
{
  "library": "AI_DropBox_V2026",
  "folder": "/Backup/Invoice",
  "sharepointHostname": "noblehomes.sharepoint.com",
  "sharepointSitePath": "/sites/PM",
  "giveUpDays": 10,
  "stallMinutes": 5
}
```

| Key | Required | Default | Notes |
|---|---|---|---|
| `library` | yes | — | |
| `folder` | key required | — | `""` = library root |
| `sharepointHostname` | yes | — | |
| `sharepointSitePath` | key required | — | `""` = root site |
| `giveUpDays` | no | `10` | range 1–365; out of range is a **400** |
| `stallMinutes` | no | `5` | range 1–1440; out of range is a **400** |
| `printerShareId` | no | *each row's `Printer_Name`* | **hard override — read §5.4 first** |

A leftover `maxRetries` key is accepted and silently ignored, *"which is what
lets the code deploy before the flows are edited"* (`deploy-to-azure.md:1201-1203`).

**200** (`function_app.py:1173-1195`) — all seven `result` values shown together
for reference; a real run returns one entry per pending row.

```json
{
  "library": "AI_DropBox_V2026",
  "folder": "/Backup/Invoice",
  "giveUpDays": 10,
  "stallMinutes": 5,
  "printerShareId": null,
  "checked": 7,
  "completed": 1,
  "failed": 1,
  "requeued": 1,
  "gaveUp": 1,
  "stillRunning": 2,
  "notFound": 0,
  "malformed": 1,
  "budgetExhausted": false,
  "printerOverridden": 0,
  "pendingFound": 7,
  "uncheckedCount": 0,
  "items": [
    { "itemId": "42", "fileName": "INV-1001.pdf", "result": "completed",
      "jobId": "1187", "message": "printed on 2026-09-04 09:12:03" },
    { "itemId": "43", "fileName": "INV-1002.pdf", "result": "failed_terminal",
      "jobId": "1188", "message": "aborted: the document could not be rendered" },
    { "itemId": "44", "fileName": "INV-1003.pdf", "result": "requeued",
      "jobId": "1189", "retry": 2, "printTime": "2026-09-04 16:35:00+00:00",
      "message": "Job Id 1189 cancelled. Retry job (2)" },
    { "itemId": "45", "fileName": "INV-1004.pdf", "result": "gave_up",
      "jobId": "1190",
      "message": "gave up after 10 day(s) and 4 retries; OUTSTANDING JOB NOT CANCELLED -- it may still print",
      "warning": "job 1190 could not be cancelled and may still print -- check the printer for a duplicate" },
    { "itemId": "46", "fileName": "INV-1005.pdf", "result": "still_running",
      "jobId": "1191", "message": "processing, job 2.3 min old (stall 5)" },
    { "itemId": "47", "fileName": "INV-1006.pdf", "result": "skipped",
      "jobId": "1192", "message": "another run requeued this file first" },
    { "itemId": "48", "fileName": "INV-1007.pdf", "result": "malformed",
      "message": "Print_JobId is set but Printer_Name is empty" }
  ]
}
```

The exact message strings, from their builders:

| Situation | Message | Built by |
|---|---|---|
| completed | `printed on 2026-09-04 09:12:03` | `printed_on_message`, `print_policy.py:725-738` |
| requeued, cancel succeeded | `Job Id 1189 cancelled. Retry job (2)` | `requeue_message` + `_cancel_clause`, `:828-869` |
| requeued, cancel failed | `Job Id 1189 CANCEL FAILED. Retry job (2)` | as above |
| requeued, no job existed | `No job to cancel. Retry job (2)` | `:843-847` |
| gave up, cancelled | `gave up after 10 day(s) and 4 retries; outstanding job cancelled` | `give_up_message`, `:849-870` |
| gave up, nothing to cancel | `… ; no outstanding job to cancel` | as above |
| gave up, cancel failed | `… ; OUTSTANDING JOB NOT CANCELLED -- it may still print` | as above |
| still running | `processing, job 2.3 min old (stall 5)` | `still_running_message` |
| still running, no age | `processing, job age unknown - not stalled` | as above |
| still running, queued | `pending, queued 42.0 min - the printer has not taken it yet` | as above — no threshold is quoted, because `pending` cannot reach one (R24) |
| warning, cancel failed | `job 1190 could not be cancelled and may still print -- check the printer for a duplicate` | `duplicate_warning`, `:872-898` |
| warning, cancel accepted but lingering | `cancel of job 1190 was accepted but it still reads 'stopped' -- it may still print` | as above |

Only `CANCEL_OK` earns the word `cancelled`; anything unrecognised falls to the
cautious branch, because *"a message that says CANCEL FAILED when the cancel
worked costs somebody a look at the printer, while the reverse hides a
duplicate"* (`print_policy.py:805-809`).

**The counter invariant, which a flow can assert:**

```
checked + uncheckedCount == pendingFound
```

Asserted at `test_second_review.py:265-266`. It exists because a crashed
submission used to appear in *no* counter at all (defect S5).

`printTime` in a `requeued` entry is the raw due time serialised by
`default=str`. The value written to the **column** is different — ISO 8601 with
its offset in `America/Vancouver`, e.g. `2026-09-04T09:35:00-07:00`
(`format_business_datetime`, `print_policy.py:679-696`). The offset must never be
dropped: the value is read back and compared, and `America/Vancouver` is `-08:00`
in January and `-07:00` in July.

### 5.3 Health — `POST {BASE}/api/print/health?code=YOUR_FUNCTION_KEY`

**Request** (`function_app.py:1260-1267`) — two keys, no library, no site.

```json
{
  "printerShareId": "5f488e73-ab80-4a6b-a60a-a0f883e17e2e",
  "printFormat": "image/pwg-raster"
}
```

`printFormat` is optional and validated exactly as Submit validates it, so a typo
is a **400 here too** — *"a malformed REQUEST is not an unhealthy PRINTER, and
conflating them would have a flow notifying about a broken device over its own
typo"* (`function_app.py:1264-1266`).

**200 — healthy** (`function_app.py:1232-1244`)

```json
{
  "healthy": true,
  "printerShareId": "5f488e73-ab80-4a6b-a60a-a0f883e17e2e",
  "printFormat": "image/pwg-raster",
  "printer": {
    "shareId": "5f488e73-ab80-4a6b-a60a-a0f883e17e2e",
    "printerId": "cf8d9fa1-0502-4b0d-b28e-22a52cdde8a1",
    "displayName": "Noble Home MFC",
    "acceptingJobs": true,
    "state": "idle",
    "contentTypes": ["image/pwg-raster"],
    "dpis": [300]
  },
  "conversion": {
    "supported": true,
    "profile": "pdf-to-pwg-raster",
    "sourceContentType": "application/pdf",
    "uploadContentType": "image/pwg-raster",
    "requestedFormat": "image/pwg-raster",
    "conversionRequired": true,
    "jobConfiguration": { "copies": 1, "dpi": 300, "…": "as in the dry run above" }
  },
  "errors": [],
  "warnings": [],
  "message": "printer Noble Home MFC is ready; documents go through the pdf-to-pwg-raster profile"
}
```

**200 — unhealthy.** Note the status code: a sick printer is never a non-2xx.

```json
{
  "healthy": false,
  "printerShareId": "5f488e73-ab80-4a6b-a60a-a0f883e17e2e",
  "printFormat": null,
  "printer": { "…": "still reported" },
  "conversion": { "…": "still reported" },
  "errors": [
    { "code": "PRINTER_STOPPED",
      "message": "the printer reports a fault (printerProcessingState: stopped)",
      "remedy": "clear the fault at the device -- paper, toner, covers, jams" },
    { "code": "NO_PRINTER_ID",
      "message": "the share reports no printer id, so a stalled job could not be cancelled before being retried -- risking a duplicate print",
      "remedy": "re-create the printer share, then update the flows with the new share id" }
  ],
  "warnings": [
    { "code": "NO_CONTENT_TYPES",
      "message": "the printer reports no content types, so the upload format cannot be verified in advance" }
  ],
  "message": "2 problems: PRINTER_STOPPED, NO_PRINTER_ID"
}
```

**200 — terminal**, where the share could not be read at all. `printer` and
`conversion` are `null` and there is exactly one error
(`function_app.py:1275-1299`):

```json
{
  "healthy": false,
  "printerShareId": "5f488e73-ab80-4a6b-a60a-a0f883e17e2e",
  "printFormat": null,
  "printer": null,
  "conversion": null,
  "errors": [
    { "code": "PRINTER_NOT_FOUND",
      "message": "…the GraphError text…",
      "remedy": "the printer share id may be stale -- read the current one from Universal Print > Printers > the printer > Overview, then update the Power Automate flows" }
  ],
  "warnings": [],
  "message": "1 problem: PRINTER_NOT_FOUND"
}
```

`remedy` is **omitted entirely** rather than sent empty when there isn't one, so
a flow can test for its presence (`print_policy.py:473-479`).

### 5.4 `printerShareId` on Poll — read before adding it

Poll accepts `printerShareId`, and when present it is a **hard override**: every
job lookup and every cancel in the run addresses that share instead of the
`Printer_Name` on each row.

> **Leave it out unless you want that rescue.** Job ids are per-printer. If the
> override ever names a share a row's job does **not** live on, the lookup and
> the cancel both 404, the 404 reads as "already gone", the row is requeued
> anyway, and the original prints **beside its replacement**. This is defect
> **F3-R** in `docs/ai/open-defects.md`, reopened deliberately.
> — `deploy-to-azure.md:1219-1224`

Exposure is **zero with one printer registered** — the override equals
`Printer_Name` on every row. The containment is the counter:
**`printerOverridden` must be 0** (`deploy-to-azure.md:1226`).

⚠️ **The repo disagrees with itself about whether Flow B should send this key.**
`design.md:177-178` includes it in Flow B's body; `deploy-to-azure.md:1090` omits
it and `:1219-1224` says to leave it out. See the appendix.

### 5.5 Errors, all three endpoints

**400 — malformed request.** Answered *before anything is claimed*, so a
corrected retry is never locked out (`function_app.py:147-151`).

```json
{ "error": "folder is required (use \"\" or \"/\" for the library root)" }
```

**500 — unexpected.** Three failures carry a `remedy` because they have a
specific fix (`function_app.py:1338-1373`):

| Cause | `remedy` |
|---|---|
| `AuthBootstrapRequired` | `run scripts/bootstrap_token.py` |
| `ColumnNotFound` | `add the missing column(s) to the SharePoint library` |
| 404 on the share preflight | the stale-share-id text quoted above |

Anything else is `{"error": "TypeName: message"}` with no remedy.

### 5.6 One caveat before pasting Flow A's body

`deploy-to-azure.md:1090` gives Flow A's body without `printFormat`, and warns
that this is only correct on a raster-only printer:

> **Flow A's body above omits `printFormat`, and that is only correct on a
> raster-only printer.** … If Health's `content` shows this printer also accepts
> `application/pdf`, add `"printFormat":"image/pwg-raster"` — otherwise Flow A
> silently runs passthrough.
> — `deploy-to-azure.md:1095-1099`

And which kind of printer this is, is **not settled in the repo** — see the
appendix. Sending `"application/pdf"` to a raster-only printer is *"a **400 on
every recurrence**, so the flow fails continuously and prints nothing"*
(`deploy-to-azure.md:1174-1176`).

---

## 6. Which value returned by Submit identifies the job for subsequent Status requests?

**None. No value returned by Submit is passed to Status, and Status accepts no
job or item identifier at all.**

This question assumes a per-job status API. This pipeline is not one. Poll's
entire request contract is seven keys (`function_app.py:870-900`):

`library` · `folder` · `sharepointHostname` · `sharepointSitePath` ·
`giveUpDays` · `stallMinutes` · `printerShareId`

There is no `jobId` parameter, no `itemId` parameter, and no route variant that
takes one. Poll queries SharePoint for **every** row with
`Print_Status = PRINT_PENDING` in the named folder, and reads `Print_JobId` off
each row — a value the app wrote there itself during Submit. The correlation is
the SharePoint row, not a token handed back to the caller.

What the returned values *are* for:

| Value | What it is | Use |
|---|---|---|
| `items[].itemId` | the SharePoint list item id | The only stable per-file correlation key. It appears in Submit's response, Poll's response, and every `PRINT_EVENT` log line, so it is what joins them |
| `items[].jobId` | the Universal Print job id | Diagnostics only. Written to `Print_JobId` by the app |

**`jobId` is per-printer, not globally unique.** From `CLAUDE.md`: *"Job ids are
**per-printer**, not globally unique. `Print_JobId` is only meaningful together
with `Printer_Name`."* `design.md:1185-1186` says the same of the log lines:
*"`item` + (`printer`,`job`) are the correlation keys — remember job ids are only
unique per printer."* A bare job id identifies nothing.

If a flow needs the state of one specific file, the answer is a SharePoint
lookup on that item, not a call to this app — which is also why there is no
reporting endpoint (see §11).

---

## 7. Which status values represent queued, processing, completed, failed, or canceled work?

**Three distinct vocabularies, deliberately not merged.** Mapping one onto
another is where this gets got wrong.

### 7.1 `Print_Status` — the SharePoint column (what the business sees)

Four values, owned by this app (`print_policy.py:58-61`):

| Value | Means | Written by |
|---|---|---|
| `PRINT_READY` | queued — waiting to be submitted | upstream, **and** Poll when it requeues |
| `PRINT_PENDING` | claimed and (normally) printing | Submit's claim |
| `PRINT_COMPLETED` | done — terminal | Poll |
| `PRINT_FAILED` | failed — **terminal, a human resets it** (`design.md:49`) | Submit or Poll |

A fifth value exists in the live library and is **out of scope**:

> **`NO_PRINT` is in live use as a `Print_Status` value** and appears nowhere in
> this codebase — something upstream writes it. That is harmless, and arguably
> useful: no route queries that value, so those files are inert and can never be
> picked up. Do not "add support" for it.
> — `deploy-to-azure.md:340-344`

**A `PRINT_PENDING` row with an empty `Print_JobId` is normal, not an error.** It
is a crashed submission — the deliberate cost of claiming before printing — and
Poll requeues it (`CLAUDE.md` rule 1; UC-6 at `design.md:899`).

### 7.2 `printJobStatus.state` — Universal Print's own job states

Eight documented values (`print_policy.py:117-124`):

`unknown` · `pending` · `processing` · `paused` · `stopped` · `completed` ·
`canceled` · `aborted`

Terminal: `completed`, `canceled`, `aborted` (`print_policy.py:132`).

⚠️ **`stopped` exists in two different enums and means different things**
(`print_policy.py:150-161`):

| Constant | Enum | Meaning |
|---|---|---|
| `JOB_STOPPED` | `printJobStatus.state` | one print job is blocked; **the job can still continue** |
| `PRINTER_STATE_STOPPED` | `printerProcessingState` | the device itself reports a fault |

*"Never substitute one for the other, and never compare a job state to a printer
state."* The first is why a stalled job must be **cancelled before it is
replaced** (`CLAUDE.md` rule 2); the second is Health's `PRINTER_STOPPED`.

### 7.3 How job state maps to what Poll does

From `poll_decision` (`print_policy.py:421-439`); the order of these checks is
load-bearing:

| Job state | Action | `Print_Status` written | Poll `result` |
|---|---|---|---|
| `completed` | complete | `PRINT_COMPLETED` | `completed` |
| `canceled` / `aborted` | fail | `PRINT_FAILED` | `failed_terminal` |
| *any*, past `giveUpDays` | give up (cancel first) | `PRINT_FAILED` | `gave_up` |
| **`pending`, at any age** | nothing — **the printer has not taken it** (R24) | *(untouched)* | `still_running` |
| non-terminal **except `pending`**, held longer than `stallMinutes` | requeue (cancel first) | `PRINT_READY` | `requeued` |
| **no job at all** | requeue — stalled immediately | `PRINT_READY` | `requeued` |
| non-terminal, inside the threshold | nothing | *(untouched)* | `still_running` |
| age undeterminable | nothing — **not** stalled | *(untouched)* | `still_running` |

**"Held longer than `stallMinutes`" is measured from `acknowledgedDateTime`** when
the job carries a usable one, otherwise from `createdDateTime` — the threshold asks
how long the *printer* has had the job, not how long ago Submit created it.

**`completed` wins over the give-up deadline.** *"A job that finished a minute
past the cut-off still put paper in the tray, and a row reading PRINT_FAILED
about a document that printed is worse than a late success."*
(`print_policy.py:398-401`)

### 7.4 Poll's per-item `result` — what the flow reads

Seven values: `completed` · `failed_terminal` · `requeued` · `gave_up` ·
`still_running` · `skipped` · `malformed`.

### 7.5 `result` in the log — a different, closed set

Nine values, and **`still_running` is deliberately absent**
(`design.md:1175-1183`):

`submitted` · `failed` · `skipped` · `completed` · `failed_terminal` ·
`requeued` · `gave_up` · `not_found` · `cancelled`

> Note what is **absent**: `still_running`. A job still in flight is not an
> outcome, it is the absence of one, and emitting a line every ten minutes for
> every unfinished job would swamp the very table the weekly counts are computed
> from. It appears in the Poll *response* (`stillRunning`), which is where a
> caller wants it, and nowhere in the log.

---

## 8. Should the flow poll Status until completion, and what polling interval and maximum duration should it use?

**No. The flow must not poll a submission to completion — there is no
per-submission poll loop anywhere in this design.**

Flow B is an **independent recurrence over the whole queue**, not a loop attached
to a Submit call. It runs every 10 minutes, reads every `PRINT_PENDING` row in
the folder, and acts on each. Submit and Poll never share a run.

The only loop in the flows is Flow A's, and it does **not** loop on completion —
it loops to drain a batch of 5 (`deploy-to-azure.md:1239-1247`):

```
Recurrence
└─ Do Until   remainingReady = 0   OR   iterations >= 10     ← always bound it
   └─ HTTP POST {BASE}/api/print/submit?code={KEY}
   └─ Parse JSON → remainingReady, submitted, failed, printerAvailable
└─ Condition: failed > 0  or  status <> 200  or  printerAvailable = false  → notify
```

> **Do not drop `printerAvailable` from that condition.** When the printer is not
> accepting jobs the run is a perfectly healthy `200` with `failed = 0`, so every
> other test passes and the flow says nothing. That flag is the only thing in the
> response that distinguishes "nothing to print" from "nothing *can* print".
> — `deploy-to-azure.md:1249-1252`

### Maximum duration for one document

`giveUpDays` — **10 days** by default, range 1–365. It is the *only* bound:

> **One bound, not two.** `giveUpDays` fails the row at 10 days and is the only
> limit. `maxRetries` used to bound the *work* separately … but the retry count
> is derived from the file's age anyway, so a count and a deadline were two
> answers to one question.
> — `design.md:687-691`

Retry `n` falls due at file age `stallMinutes × (2ⁿ − 1)` — 5, 15, 35, 75 minutes
and so on, doubling; retry 11 lands at 7.11 days and retry 12 would be 14.22 days,
**clamped to the deadline** (`design.md:691-692`). The waiting happens in Submit
against `Print_Time`, not in Poll.

### The interval — and the number nobody has

Flow B's 10 minutes is currently **a guess**, and the repo says so plainly.

> **Flow B's 10-minute cadence prevents UC-9.** Poll must run more often than
> Universal Print discards finished jobs. If a job completes and is purged before
> Poll sees it, Poll gets a 404, reads that as a stalled attempt, and requeues
> the document — printing it twice. The mitigation is entirely the cadence, and
> the cadence should be set from the measurement in **[7]**, which has not been
> taken.
> — `timing.md:238-243`

**`NOT DECIDED IN THIS REPO`** — the correct interval. Universal Print job
retention is `timing.md:191`'s *"🔴 **UNKNOWN — never measured**"* and is ranked
the project's #1 risk. `live-test.md:184-188` is blunt:

> Until then Flow B's ten-minute cadence is a **guess** — a safe-looking one, but
> UC-9's duplicate print is exactly what an unlucky guess costs.

To settle it, poll a printed job daily on the **printer** route until it 404s
(`timing.md:245-250`); `live-test.md:179-183` holds an empty table for the
readings.

### A cadence consequence worth knowing

The cadence that costs retries is **Flow A's**, not Flow B's. Flow B at 10 min
and at 1 min produce identical retry sequences because `Print_Time` pins the
instant. But the retry count is derived from the file's age when its *first* job
is created, so (`design.md:671-681`, `deploy-to-azure.md:1101-1109`):

| Flow A recurrence | Schedule opens at |
|---|---|
| 1 min | retry 1 |
| 5 min | retry 2 |
| **15 min** | **retry 3** |
| 30 min | retry 3 |

*"Lowering `stallMinutes` compresses the ladder and makes this worse, not
better."*

---

## 9. What should happen if Health reports an unhealthy service before submission or during polling?

**Flow A gates and terminates. Flow B notifies and continues anyway.** The
asymmetry is deliberate.

```
Flow A:  health  →  Condition healthy == false  →  notify, TERMINATE
                 →  else the Do Until / submit loop as before

Flow B:  health  →  Condition healthy == false  →  notify, but CONTINUE
                 →  poll anyway
```
— `deploy-to-azure.md:1123-1130`

> **Flow A gates; Flow B does not.** Poll marks completions, appends retry
> history and gives up on rows past `giveUpDays` — none of which needs a working
> printer. Skipping Poll during an outage means rows reach no terminal status for
> exactly as long as the outage lasts, which is when recovery matters most.
> — `deploy-to-azure.md:1132-1136`

### Read `healthy`, not the status code

> A sick printer is a **200 with `healthy: false`**, never a non-2xx, so the HTTP
> action succeeds and the flow keeps control of the branch. Read `healthy`;
> `errors[].code` says which of the ten conditions fired.
> — `deploy-to-azure.md:1137-1138`

The reasoning (`design.md:776-780`): Power Automate marks a non-2xx HTTP action
as *failed*, which halts the branch unless every downstream action carries a
run-after override — *"and the body, which is the whole diagnosis, becomes
awkward to read exactly when it matters."*

`400` means the request was malformed. `500` means Health itself broke. Neither
ever means "the printer is sick".

`healthy`, `errors` and `warnings` are present on **every** 200, so a flow
condition needs no null check (`design.md:782-783` — the S3 lesson).

### The codes a condition may key on

A **closed, stable vocabulary** — *"a flow condition and a KQL query both key on
them, so renaming one is a breaking change no Python would catch"*
(`design.md:789-791`). `print_policy.ALL_HEALTH_CODES` is the list.

| Errors → `healthy: false` | Warnings → still healthy |
|---|---|
| `AUTH_BOOTSTRAP_REQUIRED` · `PRINTER_NOT_FOUND` · `PRINTER_UNREACHABLE` · `PRINTER_NOT_ACCEPTING_JOBS` · `PRINTER_STOPPED` · `NO_PRINTER_ID` · `FORMAT_NOT_SUPPORTED` · `NO_PROFILE` · `JOB_CONFIGURATION_FAILED` · `CONVERTER_UNAVAILABLE` | `NO_CONTENT_TYPES` · `PRINTER_STATE_UNKNOWN` |

Health accumulates **every** finding rather than stopping at the first, *"or a
flow learns one problem per cycle and takes an hour to hear six"*
(`design.md:770-771`).

### Why Health exists at all

Three conditions are found nowhere else, or found too late to help
(`CLAUDE.md` rule 2c): `PRINTER_STOPPED` is checked nowhere else at all;
`NO_PRINTER_ID` is otherwise noticed by `cancel_job` at the moment a cancel is
attempted, by which point the duplicate is unavoidable; and
`CONVERTER_UNAVAILABLE` is otherwise discovered one claimed file at a time,
because `pypdfium2` is imported lazily inside `convert_pdf`.

---

## 10. Which HTTP responses should be retried, and how many retry attempts should be allowed?

Two different layers, and the repo decides only one of them.

### 10.1 Inside the Function App — fully decided

| | Value | Source |
|---|---|---|
| Retryable statuses | **`429`, `503`, `504`** — and nothing else | `graph_client.py:48` |
| Max attempts | **3** | `graph_client.py:47` |
| Backoff | `Retry-After` when given, else exponential with jitter | `graph_client.py:133-143` |
| `Retry-After` cap | **20 s** — longer is not worth waiting for inside a budgeted request | `graph_client.py:50-52` |
| Per-call timeout | **30 s** (`GRAPH_TIMEOUT_SECONDS`, range 1–300) | `graph_client.py:44-46` |

> **6.2 Retry and throttling.** Retry `429/503/504` honouring `Retry-After`; max
> 3; exponential backoff with jitter. Never other 4xx. **The create-job `POST` is
> attempted once** — a retry risks a duplicate job.
> — `design.md:818-820`

Calls explicitly **not** retried, via `retry=False`: creating a print job
(`universal_print.py:155`, `:170`) and cancelling one (`:372`). The
unauthenticated document download **is** retried, because a GET is idempotent and
*"nothing here can print anything"* (`graph_client.py:281-292`) — added after
local TLS interception killed two of three attempts.

A known hole worth knowing before touching these numbers
(`timing.md:215-228`):

```
   3 attempts × 30 s socket timeout   =   90 s
   2 sleeps   × 20 s Retry-After cap  =   40 s
                                          ─────
                                          130 s   for ONE call
```

> That exceeds both the 90 s budget and the 120 s connector ceiling, and the
> budget is only checked *between* files, so nothing interrupts it.

### 10.2 The Power Automate HTTP action's own retry policy

**`NOT DECIDED IN THIS REPO`.** No document mentions the HTTP connector's retry
policy, an asynchronous pattern, or a per-action timeout. There is no
recommendation here to quote.

What the repo *does* establish, which bears on the decision:

- **The timeout chain** (`timing.md:36-44`), each layer undercutting the next:
  `GRAPH_TIMEOUT_SECONDS` 30 s < `PRINT_BUDGET_SECONDS` 90 s < Power Automate
  HTTP ~120 s (fixed, Azure's) < `functionTimeout` 30 min. *"The 120 s row is the
  only one we cannot move, so it is the one everything else is sized against."*
- **A timed-out call is invisible, not failed.** *"A connector that has given up
  never receives the response, so Flow A neither loops on `remainingReady` nor
  fires its notify condition. The run is not reported as failed — it is not
  reported at all."* (`timing.md:200-202`)
- **Concurrent runs are safe by design.** UC-7 (`design.md:900`): *"Flow A
  schedules overlap; two runs start together. Both pick the same files; `If-Match`
  means exactly one wins per file, the loser records `skipped`. **No file printed
  twice.**"*
- **A 400 claims nothing.** UC-11 (`design.md:904`): *"400 with a specific message
  and **zero** SharePoint writes, so the corrected retry is not locked out."*
- **Health writes nothing**, so it is *"safe to call as often as a flow likes"*
  (`function_app.py:1254-1257`).

---

## 11. Where should completed results be written or returned?

**Onto the file itself, in five SharePoint columns, written exclusively by the
Function App.** No flow may write them.

| Column | Type | Holds |
|---|---|---|
| `Print_Status` | single line of text, **INDEXED** | the four status values |
| `Print_JobId` | single line of text | the Universal Print job id — meaningful only with `Printer_Name` |
| `Printer_Name` | single line of text | the printer **share id** the job went to |
| `Print_Message` | single line of text | `printed on YYYY-MM-DD HH:MM:SS`, or the failure reason tagged with the stage |
| `Print_Time` | Date and Time, **Include Time on**, Friendly format off, not indexed | when the next attempt falls due; set **only** on a `PRINT_READY` row |

`Print_Status` **must be indexed** — a non-indexed column cannot be used in a
Graph `$filter` at all, so without it every query fails. Internal names are
resolved at runtime and must never be hardcoded (`design.md:814-816`).

### The write matrix — the complete contract

Reproduced from `design.md:384-396`. `—` means the column is not touched. *"This
table **is** the Tier C test suite: one assertion per row."* (`design.md:398`)

| Event | `Print_Status` | `Printer_Name` | `Print_JobId` | `Print_Message` | `Print_Time` |
|---|---|---|---|---|---|
| Submit — claim | `PRINT_PENDING` | share id | cleared | **preserved** | **cleared** |
| Submit — job started | — | — | job id | — | — |
| Submit — failed (download/create/upload/start) | `PRINT_FAILED` | share id | — (stays empty) | error text, truncated | **cleared** |
| Submit — claim lost (412) | — | — | — | — | — |
| Submit — not yet due (`Print_Time` in the future) | — | — | — | — | — |
| Poll — `completed` | `PRINT_COMPLETED` | — | — | `printed on YYYY-MM-DD HH:MM:SS` | **cleared** |
| Poll — `canceled` / `aborted` | `PRINT_FAILED` | — | — | job `description` + `details` | **cleared** |
| Poll — in flight, inside the stall threshold | — | — | — | — | — |
| **Poll — stalled** | `PRINT_READY` | — | **cleared** | **append** `Job Id N <cancel outcome>. Retry job (n)`, or `No job to cancel. Retry job (n)` when there was none | **next due time** |
| **Poll — past the give-up threshold** | `PRINT_FAILED` | — | — | **append** the give-up reason, **including what the cancel achieved** | **cleared** |

The last two rows are the only places the app **appends** to `Print_Message`
rather than replacing it. `Print_Message` is capped at 255 characters
(`print_policy.py:102`), and `printed on` is rendered in `America/Vancouver`
(`print_policy.py:101`).

### What is returned to the flow

Only the counters and the per-item array documented in §5 — a summary of *that
invocation*. There is no way to ask the app for a file's state afterwards, on
purpose:

> **Why no `/api/print/report` endpoint.** It would only re-expose what SharePoint
> already answers exactly, and what Power Automate can already read with a
> connector action. Adding an endpoint would create a second way to ask the same
> question, with its own bugs and tests.
> — `design.md:1307-1311`

### The two-store split

| Question | Answered by |
|---|---|
| *"What is the state **right now**?"* | **SharePoint** — the five columns *are* the state |
| *"What **happened** over time?"* | **Application Insights** — the columns hold only the latest state; *"A file retried three times looks identical to one retried once"* |

— `design.md:1120-1126`

---

## 12. Where should errors and diagnostic details be recorded, and who should receive failure notifications?

**Recorded in three places. Who receives the notifications is not decided in this
repo.**

### 12.1 On the file — `Print_Message`

The failure reason, tagged with the stage that failed, or the give-up reason
including what the cancel achieved. This is what a person sees in the library
without opening any tooling.

One thing deliberately **not** written there: a lingering cancel. *"A lingering
job — the cancel accepted but the job still reporting a live state — is reported
as a `warning` on the run's response item and an `ERROR` log line, **not** in
`Print_Message`"* (`design.md:413-417`), because the message states fact and a
lingering state is a snapshot that may resolve a second later.

### 12.2 In Application Insights — two log lines

**Sampling must stay disabled in `host.json`** (`design.md:1188-1196`) — adaptive
sampling silently drops `traces` rows, which would make every count quietly
wrong with no error anywhere.

Free-text fields go **last** so the KQL `parse` stays unambiguous
(`design.md:1157-1167`):

```
RUN_SUMMARY  ep=submit lib=Documents printer=<shareId> found=23 ok=4 failed=1
             skipped=0 remaining=18 httpStatus=200 ms=8421 folder=/Invoices/ToPrint

PRINT_EVENT  ep=submit item=42 from=PRINT_READY to=PRINT_PENDING job=1825
             printer=<shareId> result=submitted ms=1420 file=Invoice 2026-08 Acme.pdf
```

Changing a field name or its position breaks the workbook silently. On
`ep=health`, `RUN_SUMMARY` reuses existing fields rather than adding any: `ok` is
`1`/`0` for healthy, `failed` is the error count, `skipped` the warning count,
and `lib`/`folder`/`found`/`remaining` are sentinels (`design.md:1141-1148`).
**Health emits no `PRINT_EVENT`** — it touches no files.

The query pack is `design.md:1198-1274`; query 6 answers *"did the schedule
stop?"*, and the workbook build is `design.md:1276-1290`.

### 12.3 Azure Monitor alerts

Four rules, all pointed at **one action group with an email target**
(`design.md:1313-1323`):

| Name | Scope | Condition | Sev | Catches |
|---|---|---|---|---|
| `alert-print-5xx` | Function App | `Http Server Errors` ≥ 3 in 1 h | 2 | Graph/auth/printer breakage |
| `alert-print-silent` | App Insights | Query 6 returns no `submit` row in 2 h | 2 | Flow off, connection expired, token revoked — **the failure mode with no other trace** |
| `alert-print-failed-batch` | App Insights | `PRINT_EVENT … result=failed` ≥ 5 in 1 h | 3 | A bad printer or a run of bad documents |
| `budget-print` | Resource group | 80 % actual / 100 % forecast | — | Cost guardrail |

The runbook's minimum is the **silence** alert, *"because a stopped flow produces
no errors at all, so it is the one failure with no other signal"*
(`deploy-to-azure.md:1268-1278`).

Also available and often forgotten: **Power Automate run history** holds each
flow run and its retries for 28 days — the store that answers *"did the schedule
actually fire?"* (`design.md:1133`).

### 12.4 The in-flow notify conditions — what must fire

These *are* specified, verbatim:

| Flow | Condition | Source |
|---|---|---|
| A | `healthy == false` → notify, **TERMINATE** | `design.md:156` |
| A | `failed > 0 OR status != 200 OR printerAvailable == false` → notify | `design.md:166` |
| B | `healthy == false` → notify, but **CONTINUE** | `design.md:173` |
| B | `status != 200` → notify | `design.md:179` |
| B | `printerOverridden > 0` → notify (F3-R) | `design.md:180`, `deploy-to-azure.md:1233` |

For the last one, the WARNING in the log is graded and worth reading: a
disagreeing row **with** an outstanding job names F3 and can genuinely print
twice; one **without** a job is only a configuration mismatch
(`deploy-to-azure.md:1226-1230`).

### 12.5 Who receives them

**`NOT DECIDED IN THIS REPO`.** Every flow sketch ends with the word `notify` and
no document names a connector, a recipient, or a message template. Teams appears
nowhere in the repository. The only channel named anywhere is the Azure Monitor
action group's **email** target (`design.md:1322`), which covers the four alert
rules above — not the in-flow conditions.

The conditions in §12.4 are settled; the routing is an open decision.

---

## Appendix A — open items

| Item | Status | Where it would be decided |
|---|---|---|
| Notification channel and recipient for the in-flow `notify` steps | `NOT DECIDED IN THIS REPO` | The flows, plus a line in `design.md` §3 |
| Power Automate HTTP action retry policy, async pattern, per-action timeout | `NOT DECIDED IN THIS REPO` | The flows, plus `timing.md` §6 |
| Universal Print job retention — and therefore Flow B's cadence | **Unmeasured.** `timing.md:191` marks it 🔴 *"UNKNOWN — never measured"* and ranks it risk #1 | Measure per `timing.md:245-250`; log in `live-test.md:179-183` |
| Whether the current printer accepts `application/pdf`, deciding whether Flow A needs `printFormat` | Unresolved — see Appendix B | A live read: `.\scripts\live-printer-check.ps1 -DiagnoseOnly` |
| The deployed app's literal hostname | Not recorded, by design (hashed for Flex Consumption) | `az functionapp show … --query defaultHostName` |

## Appendix B — where the repo contradicts itself

Three places. None is adjudicated here; each needs a decision or a live read.

**1. Whether Flow B's body should carry `printerShareId`.**

| Says include it | Says leave it out |
|---|---|
| `design.md:177-178` — the Flow B sketch has `"printerShareId":"<guid>"` in the body | `deploy-to-azure.md:1090` — the flow table omits it entirely |
| | `deploy-to-azure.md:1219-1224` — *"**Leave it out unless you want that rescue.**"* |

This is not cosmetic: the key is the precondition for defect F3-R, where the
original job *"prints beside its replacement"*. Exposure is zero with one printer
registered, and `printerOverridden` is the containment.

**2. Whether the live printer accepts `application/pdf`.**

`design.md:163-164` asserts, inside Flow A's sketch, that *"the live printer takes
image/pwg-raster ONLY, so naming application/pdf would 400 every run"*. But the
runbook says that answer is not evidence:

> **The repo currently disagrees with itself** and must not be trusted for this:
> `e2e-testing.md:211` says this printer reports both, while its own §B5a callout
> and `README.md` describe the **retired** Brother MFC-L5800DW … Only a live read
> is evidence.
> — `deploy-to-azure.md:185-190`

It decides whether Flow A's body needs `"printFormat":"image/pwg-raster"`. On a
dual-format printer, omitting it silently runs `passthrough` — a pipeline nothing
has bench-tested. On a raster-only printer, sending `application/pdf` is a 400 on
every recurrence.

**3. Stale text naming a retired flow.** UC-12 at `design.md:905` still ends
*"Flow C's notification fires"*, although `design.md:186-187` retires Flow C and
`deploy-to-azure.md:1111` says calling it returns 404. The failure it describes —
a revoked refresh token — is real; the flow that would notice it no longer
exists, and nothing replaced that notification (`timing.md:235-236`).

---

## Related reading

| File | For |
|---|---|
| `docs/design.md` | §3 the flows · §5.4 the write matrix · §13 reporting |
| `docs/deploy-to-azure.md` | §10 the runbook version of the flows, with live values |
| `docs/timing.md` | every clock, knob and unmeasured number |
| `docs/live-test.md` | testing against the real printer |
| `docs/ai/open-defects.md` | F3-R, G3 and the rest |
| `docs/ai/troubleshooting.md` | confirmed mistakes and verified fixes |
