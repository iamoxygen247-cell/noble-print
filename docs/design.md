# noble-print — SharePoint → Universal Print utility

> **First implementation step:** create `noble-print/docs/` and save this file there as
> `docs/design.md`. Plan mode is read-only except for the plan file. Also copy
> `docs/project-playbook.md` from `noble-invoice-process` into `docs/ai/project-playbook.md`
> (cited below as §P-n).

---

## 1. Context

Noble Homes has a SharePoint document library whose files carry five columns:
`Print_Status`, `Print_JobId`, `Print_Message`, `Printer_Name` and `Print_Time`. The first four
are the requirement's; `Print_Time` was added on 2026-09-02 to make the retry backoff visible
(R21). An upstream process drops files in with `Print_Status = "PRINT_READY"`. They must reach a physical printer through Microsoft
Universal Print, with the outcome written back onto each file so **the library is both the work
queue and the audit trail** — no separate database.

This repo (`noble-print/`, empty today) holds a Python Azure Function App with three named HTTP
endpoints that Power Automate calls on a schedule.

**Design goal beyond the immediate requirement:** this is a *utility*, expected to be reused by
other workflows. Reuse comes from **layering and single-source-of-truth constants** (§P-2.2), not
a runtime configuration engine — see §9. Volume/traffic scaling is explicitly a non-goal.

### Endpoint names

| Name | Route | Responsibility |
|---|---|---|
| **Health** | `POST /api/print/health` | Report whether the printer can be used at all. Reads one share, writes nothing |
| **Submit** | `POST /api/print/submit` | Claim the oldest `PRINT_READY` files **that are due** (`Print_Time` empty or past) and create print jobs |
| **Poll** | `POST /api/print/status` | Check `PRINT_PENDING` jobs: mark the finished, requeue the stalled, fail the hopeless |

Referred to by name throughout — never "endpoint 1/2/3".

> **Resubmit was retired on 2026-09-01.** A third endpoint, `POST
> /api/print/resubmit`, ran daily and would not touch a file until it was 72 hours
> old. Its work moved into Poll on a five-minute exponential schedule. R12–R17
> described it and are retired with it; see §2.

### Agreed decisions

| Question | Choice |
|---|---|
| Universal Print auth | Refresh token in Key Vault, device-code bootstrap, rotated on use |
| Printer identifier | **Printer share id** → `/print/shares/{id}/...` |
| Batch model | Small synchronous batch (default **5**, max 15); the flow loops on `remainingReady` |
| `Print_Message` clock | `America/Vancouver` |
| Recovery scope | **`PRINT_PENDING` only.** `PRINT_FAILED` is terminal — a human resets it |
| Reuse mechanism | Fixed constants in one module; no profile/config engine |
| Job configuration | Fixed `{"copies": 1}`; printer defaults decide duplex/colour/paper |
| Poll on canceled/aborted | Mark `PRINT_FAILED` immediately |

---

## 2. Requirements trace

| # | Requirement | Where | Notes |
|---|---|---|---|
| R1 | Four columns exist | §5.4 write matrix | Internal names resolved at runtime by `displayName` (§6.2). ⚠️ **A fifth was added:** `Print_Time`, see R21 |
| R2 | An endpoint Power Automate can call | **Submit**, §3 | Function-key auth |
| R3 | Retrieve the **oldest 15** `PRINT_READY` files from a library | **Submit** step 4 | ⚠️ **Deviation:** default batch **5**, range 1–15, per the agreed loop model. `"batchSize": 15` reproduces the literal wording |
| R4 | Call Universal Print to create a print job | **Submit** 5.3–5.6 | 4 calls: create → upload session → upload → start |
| R5 | On success → `PRINT_PENDING`, `Printer_Name` = printer id, `Print_JobId` = job id | §5.4 | ⚠️ **Ordering change:** status+printer written *before* submission as an atomic claim; job id after. End state identical. Rationale §5.3 |
| R6 | On failure → `PRINT_FAILED`, `Printer_Name`, `Print_Message` = the error | §5.4 | ⚠️ Message carries the error from **whichever step failed**, not only Universal Print's text — a SharePoint-side failure must not be reported as a printer fault |
| R7 | Inputs: folder, library, PrinterId | **Submit** request | Plus optional `batchSize`, and — since R23 — the required `sharepointHostname` / `sharepointSitePath` |
| R8 | A second endpoint to check job status | **Poll** | |
| R9 | Query the folder, **last 20 days**, `PRINT_STATUS = "PRINT_PENDING"` | **Poll** step 1 | Window on `createdDateTime`, consistent with R16 |
| R10 | `COMPLETED` → `PRINT_COMPLETED` | **Poll** step 3 | |
| R11 | `Print_Message` = when it printed, `printed on 2026-08-01 14:23:23` | **Poll** step 3 | ⚠️ `printJob` has **no completion field**, so no exact answer exists. Uses the printer's own `acknowledgedDateTime`, falling back to the moment Poll observed completion; rendered in `America/Vancouver`. Documented, not fabricated (§P-3.5). **Revised — see below.** |
| ~~R12~~ | ~~A third endpoint to re-submit outstanding jobs~~ | **RETIRED** | No third endpoint exists. Recovery is Poll's |
| ~~R13~~ | ~~`PRINT_PENDING` **or** `PRINT_FAILED`~~ | **RETIRED** | Poll queries `PRINT_PENDING` only. **`PRINT_FAILED` is now terminal** — nothing retries it |
| ~~R14~~ | ~~Past 20 days~~ | **SUPERSEDED** | `giveUpDays`, default 10. It now *fails* the row rather than dropping it — this is what closes G1 |
| ~~R15~~ | ~~Older than 72 hours~~ | **SUPERSEDED** | The exponential schedule: 5, 15, 35, 75 … minutes |
| **R16** | Initial print request time = SharePoint file creation time | **Poll — `poll_decision`** | ✅ **Still in force.** The schedule reads `createdDateTime`; our own writes bump `lastModifiedDateTime`, so driving it from that would reset the file to age zero on every requeue and the retries would stop after the first |
| ~~R17~~ | ~~"Outstanding = `PRINT_STATUS` ≠ `PRINT_COMPLETED`"~~ | **MOOT** | The contradiction was about which statuses a second query should sweep. There is one query |
| R18 | Python | throughout | Azure Functions Python v2 model |
| R19 | Reuse learnings/structure from `noble-invoice-process` | §8, §9, §13 | Playbook §§2.1–2.8, 5–7 applied |
| R20 | Testing in the design | §10 | Five tiers |
| **R21** | A stalled `PRINT_PENDING` job records **when** it will be retried, in `Print_Time`, and still returns to `PRINT_READY` | **Poll** check 4, §5.4, §5.7 | The retry time is the existing ladder, `stallMinutes × (2ⁿ − 1)` from `createdDateTime`, clamped to the give-up deadline. Written in the business zone **with its UTC offset** — see §5.7 |
| **R22** | Submit processes only files where `Print_Status = PRINT_READY` **and** `Print_Time <= Now` | **Submit** step 4 | Compared in Python, not `$filter` (one indexed field at a time). ⚠️ **A blank or unreadable `Print_Time` means DUE NOW** — upstream files carry none, and a human resetting a terminal row leaves none either; fail-closed would stop the queue dead |
| **R23** | The site is named by the caller, not the deployment | **Submit** / **Poll** request | `sharepointHostname` + `sharepointSitePath` replace two app settings. Missing → **400**, previously a 500. Widens blast radius by design: §5.8 |

> **R11 revised 2026-08-30, after reading a real job back.** The original note said Universal Print
> "returns no completion timestamp" and used that to justify stamping `Print_Message` with the moment
> Poll happened to look. The first half is still true — Microsoft's `printJob` reference lists no
> completion property. The second half did not follow from it. Reading job `6` out of the live service
> showed `acknowledgedDateTime`, documented as "the dateTimeOffset when the job was acknowledged",
> which nobody had noticed because `FakeGraph` did not model the field — so every test in the suite
> agreed with an incomplete picture of the resource.
>
> The two candidates are not equally good:
>
> | Source | Belongs to | Measured on job `6` |
> |---|---|---|
> | `acknowledgedDateTime` | the **job** — stable, same answer on every re-read | 05:25:03Z, ~10 s before the page finished |
> | the moment Poll looked | **our cron** — moves if the schedule changes | up to a full 10-minute interval late |
>
> Poll now prefers the acknowledgement and falls back to the observed time when a job carries none.
> Poll is now the ONLY writer of this column, so the class of disagreement this note guarded against
> (two paths writing one column) is gone by construction. Kept because two paths writing one column must
> agree. `tests/test_acknowledged_time.py`.
>
> The lesson is about the fake, not the field: **a fixture that omits a property teaches everyone the
> property does not exist.** `FakeGraph.add_job` now models `acknowledgedDateTime`, `isFetchable` and
> `errorCode`.

### Gaps in the requirement, surfaced

- **G1 — The 20-day cliff. FIXED 2026-09-01.** A file stuck at `PRINT_PENDING` beyond 20 days used
  to be excluded by **both** Poll and Resubmit and become permanently invisible — no status change,
  no alert, and only a `staleCount` field to hint at it. Poll now examines **every** pending row and
  writes `PRINT_FAILED` with a reason past `giveUpDays`, so no row strands *for age*. The `staleCount` /
  `staleItems` fields and the `test.py stale` command are gone with the hole they described.
- **G2 — `Printer_Name` holds an id, not a name.** Followed literally. The preflight already reads
  the share's `displayName`, so storing the friendly name later is a one-line change.
- **G3 — Non-printable files.** Handled: the file's content type is checked against the share's
  `capabilities.contentTypes` **before** the job is created, with a clear `Print_Message` instead
  of an opaque upload error.
- **G4 — Nothing sets `PRINT_READY`.** Out of scope by design; an upstream process owns it.
- **G5 — Reporting.** The requirement defines no way to see aggregate state. Addressed in §13.

---

## 3. Responsibility split — Power Automate vs Function App

**Power Automate decides *when* and *how often*. The Function App decides *what happens*.**
Any drift across this line is a bug.

| Concern | Owner |
|---|---|
| Schedule / recurrence | **Power Automate** |
| Looping until the queue drains | **Power Automate** (on `remainingReady`) |
| Supplying library, folder, printer share id | **Power Automate** |
| Operational alerting and the weekly digest | **Power Automate** |
| Microsoft Graph calls (SharePoint *and* Universal Print) | **Function App** |
| Token acquisition, caching, refresh-token rotation | **Function App** |
| Deciding any `Print_Status` value | **Function App** |
| Writing any of the five columns | **Function App** — *exclusively* |
| Claiming / idempotency / double-print prevention | **Function App** |
| Retry, throttling, `Retry-After` | **Function App** |
| Date windows, 72-hour rule, oldest-first ordering | **Function App** |
| Batch sizing and the wall-clock budget | **Function App** |

> **Single-writer rule (§P-2.4).** The flow must **never** patch the five columns itself, even for
> a quick fix. Two writers to one field is a race debugged at 2 a.m. If the flow needs a state
> changed, it calls an endpoint.

### The Power Automate flows

**Flow A — Print Submit** (every 15 min)
```
Recurrence
└─ HTTP POST {funcUrl}/api/print/health?code={key}
         {"printerShareId":"<guid>"}          ← same printer keys Flow A sends
└─ Condition: healthy == false  →  notify, TERMINATE (submit nothing)
└─ Do Until  (remainingReady == 0)  OR  (iterations >= 10)      ← loop guard, never unbounded
   └─ HTTP POST {funcUrl}/api/print/submit?code={key}
            {"sharepointHostname":"<tenant>.sharepoint.com",
             "sharepointSitePath":"/sites/<site>",
             "library":"Documents","folder":"/Invoices/ToPrint",
             "printerShareId":"<guid>","batchSize":5}
   (printFormat is optional and omitted here on purpose: the live printer takes
    image/pwg-raster ONLY, so naming application/pdf would 400 every run)
   └─ Parse JSON → remainingReady, submitted, failed
└─ Condition: failed > 0 OR status != 200 OR printerAvailable == false  →  notify
   (the last term is load-bearing: an offline printer is a 200 with failed = 0)
```

**Flow B — Print Status Poll** (every 10 min)
```
Recurrence → HTTP POST /api/print/health {"printerShareId":"<guid>"}
          → Condition: healthy == false → notify, but CONTINUE. Poll marks
            completions and gives up on old rows whether or not the printer
            is well; skipping it during an outage is when recovery matters most
          → HTTP POST /api/print/status
            {"sharepointHostname","sharepointSitePath","library","folder",
             "printerShareId":"<guid>","giveUpDays":10,"stallMinutes":5}
          → Condition: status != 200 → notify
          → Condition: printerOverridden > 0 → notify  (see F3-R, docs/ai/open-defects.md)
```
Cadence is load-bearing: Poll must run more often than Universal Print discards finished jobs, or
a completed job disappears before Poll sees it and Poll requeues it (UC-9). Measure that
retention window during rollout (§4 open items) and set the cadence from the measurement.

**Flow C — retired.** Recovery moved into Flow B. Delete this flow; leaving it
running will 404 every night.

**Flow D — Weekly digest** (Mondays 07:00) — see §13.7.

---

## 4. Verified against the source

Read from Microsoft's documentation, not recalled.

### Universal Print

| Fact | Source |
|---|---|
| Creating a print job is **delegated-only** — `Application: Not supported` on both routes. The sole reason for the Key Vault refresh-token design. | [share: create job](https://learn.microsoft.com/en-us/graph/api/printershare-post-jobs?view=graph-rest-1.0), [printer: create job](https://learn.microsoft.com/en-us/graph/api/printer-post-jobs?view=graph-rest-1.0) |
| `printJob: start` documented **only** for the share route; `Application: Not supported`. | [printJob: start](https://learn.microsoft.com/en-us/graph/api/printjob-start?view=graph-rest-1.0) |
| `createUploadSession` on a share is "supported with **delegated permissions only**". | [createUploadSession](https://learn.microsoft.com/en-us/graph/api/printdocument-createuploadsession?view=graph-rest-1.0) |
| **`printJob: cancel` is `POST /print/printers/{printerId}/jobs/{printJobId}/cancel` → `204`.** Documented **only on the printers path** — it needs a *printer* id, not a share id. Delegated-only. "For an app… to cancel **other users'** jobs, the signed-in user must be a member of the Printer Administrator role" — our service account cancels its **own** jobs, so no admin role. | [printJob: cancel](https://learn.microsoft.com/en-us/graph/api/printjob-cancel?view=graph-rest-1.0) |
| Reading a job **does** support app-only (`PrintJob.ReadBasic.All`). | [Get printJob](https://learn.microsoft.com/en-us/graph/api/printjob-get?view=graph-rest-1.0) |
| States: `unknown, pending, processing, paused, stopped, completed, canceled, aborted`. Terminal: **completed, canceled, aborted**. **`stopped` = "an issue with the printer needs to be addressed before the job can continue" — i.e. it can still resume.** A new job is `paused` / `uploadPending`. | [printJobStatus](https://learn.microsoft.com/en-us/graph/api/resources/printjobstatus?view=graph-rest-1.0) |
| **The upload `PUT` must NOT carry `Authorization`** — "might result in an `HTTP 401`". `uploadUrl` is opaque and carries its own `tempauthtoken`. | [Upload documents](https://learn.microsoft.com/en-us/graph/upload-data-to-upload-session) |
| **< 10 MB** per `PUT`; ranges a multiple of 200 KB; `202` + `nextExpectedRanges` while more remain, **`201` on the last**; `416` if already received; `DELETE` cancels. Log `X-MSEdge-Ref`, `request-id`. | same |
| `contentType` must be supported by the printer — check `capabilities.contentTypes`. | [createUploadSession](https://learn.microsoft.com/en-us/graph/api/printdocument-createuploadsession?view=graph-rest-1.0) |
| `printerShare` exposes `capabilities`, `isAcceptingJobs`, `status` as properties **and a `printer` relationship** — one preflight call yields everything, including the printer id needed for cancel. | [printerShare](https://learn.microsoft.com/en-us/graph/api/resources/printershare?view=graph-rest-1.0) |

### SharePoint / Graph

| Fact | Source |
|---|---|
| **`if-match` is supported** on `PATCH .../items/{id}/fields`; a mismatch returns `412` **and the item is not updated** — the claim is genuinely atomic. | [Update listItem](https://learn.microsoft.com/en-us/graph/api/listitem-update?view=graph-rest-1.0) |
| `listItem` inherits **`eTag`** and **`createdDateTime`**, and has a **`driveItem` relationship**. | [listItem](https://learn.microsoft.com/en-us/graph/api/resources/listitem?view=graph-rest-1.0) |
| `$filter` on `fields/*` allows only `eq, ne, lt, gt, le, ge, startswith`, "one indexed field at a time"; `$filter` + `$expand=fields(select=…)` combine (the doc's Example 2). `$orderby` is **not** listed. | [List items](https://learn.microsoft.com/en-us/graph/api/listitem-list?view=graph-rest-1.0) |
| **Non-indexed columns cannot be used in `$filter`/`$orderby`**; `Prefer: HonorNonIndexedQueriesWarningMayFailRandomly` permits it but "may fail randomly" → **`Print_Status` must be indexed**. | [Q&A: orderby](https://learn.microsoft.com/en-us/answers/questions/821510/microsoft-graph-sharepoint-lists-api-orderby-not-w) |
| `GET /sites/{s}/lists/{list-title}` accepts a **title**; `?expand=columns(select=name,displayName)` returns column definitions in the same call. | [Get a list](https://learn.microsoft.com/en-us/graph/api/list-get?view=graph-rest-1.0) |
| Content download returns **`302`** to a preauthenticated URL — "You don't need to include an `Authorization` header", and it "might expire within minutes". | [Download content](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0) |
| Internal names encode specials as `_xHHHH_` (documented: `_x0020_`, `_x003a_`), fixed at creation. | [Naming guidelines](https://pnp.github.io/community-docs/articles/sharepoint-naming-guidelines.html) |
| `Sites.Manage.All` (application) is required if the list has **content approval** on. | [Get listItem](https://learn.microsoft.com/en-us/graph/api/listitem-get?view=graph-rest-1.0) |

### Identity and platform

| Fact | Source |
|---|---|
| Refresh token lifetime **90 days** (SPA / email-OTP: 24 h). | [Refresh tokens](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens) |
| "Refresh tokens replace themselves… **the platform doesn't revoke old refresh tokens** when used to fetch new access tokens." | same |
| Refresh tokens are **not tied to a resource** — one token covers print *and* Sites scopes. | same |
| Revoked by password change, SSPR, admin reset, explicit revocation. Password *expiry* alone does not. | same |
| `Key Vault Secrets User` = read only; **`Key Vault Secrets Officer`** = any secret action incl. `set` → Officer required, because rotation writes back. | [Key Vault RBAC](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide) |
| **Application Insights tables keep data 90 days at no charge** (`AppTraces`, `AppRequests`, `AppExceptions`, …); extendable to 730 days at cost. 90 days ≈ 13 weeks of weekly reporting, free. | [Data retention](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-retention-configure) |

### Corrections to statements I made earlier

1. **Over-claimed on column naming.** I said `Print_Status` is "almost certainly"
   `Print_x005f_Status` internally. Microsoft documents `_x0020_` and `_x003a_` but **not**
   underscore encoding; only community sources report the `_x005f_` form. Plausible, unverified
   for your tenant. The design resolves names at runtime, so it holds either way, and `--dry-run`
   prints what it found.
2. **Blob lease removed.** I proposed serialising refresh-token rotation with a blob lease. The
   platform does not revoke old refresh tokens on rotation, so concurrent refreshes both succeed
   and last-write-wins stores a valid token. One component deleted (§P-2 *Simplicity First*).
3. **Recovery had a double-print bug.** My earlier design replaced a `stopped` job without
   cancelling it. `stopped` means the printer needs attention **and the job can continue** — so
   when the printer is fixed, the old job *and* the new one both print. Fixed in §5.7: **cancel
   the old job before creating a replacement.** This is a correctness fix, not a refinement.

### Open items — verified at implementation, each with a fallback

| Item | Fallback |
|---|---|
| Does `$expand=fields(...),driveItem(...)` work in one call? | `GET .../items/{id}/driveItem?$select=…` per item — documented; the batch is 5 |
| Is `fields.FileDirRef` returned for folder scoping? | the driveItem's `parentReference.path` |
| Does cancel also work on the `/print/shares/...` path? | Use the documented printers path with the printer id from the share's `printer` relationship |
| ~~Is content approval on?~~ **CLOSED 2026-08-31 — No** | Read from *Library settings → Versioning settings*: content approval **off**, require check-out **off**, major versions only. So Graph returns every item, no `_ModerationStatus` filter is needed, and `Sites.Manage.All` (the row above) does not apply. Both settings that could have broken this pipeline are off; re-check if anyone changes versioning later. |
| Is `Print_Status` indexed? | `--dry-run` checks it before the first real run |
| ~~What type is `Print_Time`?~~ **CLOSED 2026-09-02 — `Date and Time`** | Created with *Include Time* on and *Friendly format* off; **not** indexed, and it does not need to be (§5.4). Still to prove against the live list: that Graph accepts the offset-bearing ISO string, returns the same instant, and that `null` empties the column — `scripts/verify_print_time.py` does all three on one nominated row. FakeGraph cannot answer it, because the fake stores whatever it is handed. Fallback if any of the three fails: a single-line-of-text column, with no code change — every comparison already happens in Python. |
| ~~Does the printer accept `application/pdf`?~~ **CLOSED 2026-08-30, against the live API** | `live-printer-check.ps1` reads the capability list from Graph: `application/pdf` **and** `application/oxps`. PDFs go to the device as-is. Note the correction it forced: the fixture previously recorded `["application/pdf"]` alone, copied from the portal's *Properties > Printer defaults* page — which shows the **default** content type, not the capability list. Two different fields; the portal does not label which is which. |
| ~~Are the share id and printer id really different?~~ **CLOSED 2026-08-30** | Confirmed live: the share id and the printer id are two different GUIDs. F3's whole premise, now evidenced rather than reasoned — see the current pair in `README.md`. **They also differ in lifetime.** Deleting and re-creating a share mints a new share id while the printer id and its `registeredDateTime` are untouched, so the id the caller supplies on every request is the perishable one and the id the app resolves for itself at preflight is the durable one. Every recorded copy of a share id (README, the docs, `live-printer-check.ps1`'s default, and **the Flow bodies in Power Automate**) goes stale at that moment, with a 404 reading `does not match any registered printers`. In the app it surfaces from `get_share` — the preflight — so it is a per-run 500 with `RUN_SUMMARY … http_status=500` and **nothing claimed and nothing printed**, which is the right shape. It is not yet one of the two failures `_server_error` gives a named remedy; if it recurs, that is where the remedy line belongs. **And the printer can be replaced too** — it happened on 2026-08-31, so both halves of the pair changed within hours. The durable identifier is neither GUID; it is the **display name** a human recognises, which is why every recorded id needs the printer named beside it. |
| ~~What `status.state` does the share report?~~ **CLOSED 2026-08-30** | `idle`. The portal *displays* "Ready"; `printerProcessingState` is documented `unknown\|idle\|processing\|stopped` and has no `ready` member. The fixture and two tests had recorded the portal's display string as an API value. **Health branches on it since 2026-09-01** — `PRINTER_STOPPED` is an error, by explicit decision. That is a deliberate bet on an enum this tenant has never reported as anything but `idle`, and the risk is that a transient fault which today queues work that prints on recovery (UC-8) will instead halt Flow A. **Still to be recorded: what the field actually reads with the printer switched off** — `e2e-testing.md` §B0a captures it. Everything else still only reports the value. |
| ~~**Is a Universal Print connector required?**~~ **CLOSED 2026-08-30 — no** | The printer is **Universal Print ready and registered directly**. Settled by printing: `live-printer-check.ps1` submitted a job while the API reported 0 connectors on the printer and 0 tenant-wide, and a page came out — job `6`, `pending` → `processing` → `completed` in ~5 s. Two earlier answers were wrong: the Overview blade cannot decide it (a connector's heartbeat updates last-seen too), and neither can a connector count of 0 (that is what both cases look like). **Consequence: no Windows host in the path** — nothing to keep powered on, nothing to patch, no single point of failure. |
| **How long does Universal Print keep a finished job readable?** STILL OPEN — but now cheap to measure | Sets Poll's cadence, and Poll's cadence is what prevents UC-9's double print. **Nothing has been measured on the current printer** — the one earlier reading was taken on a device since retired, so the count starts from zero. Once an invoice prints on the MFC-L5800DW, poll its job daily on the **printer** route (job ids are per-printer, and the printer id survives a re-share): <br>`GET /print/printers/{printerId}/jobs/{jobId}` <br>The **last day it still returns 200** is the retention window; set Poll's interval well inside it. A 404 is only evidence when the job was created on this printer through the current share — any other 404 says "that object is gone", not "retention expired". The first such job cannot exist until the PDF → PWG-raster conversion ships, because this device accepts `image/pwg-raster` only. Completion itself is fast — a page finishes in seconds — so the risk is never "Poll ran too early", only "Poll ran after the record was purged". |

---

## 5. System design

### 5.1 Components

```
┌──────────────────┐   4 scheduled flows,   ┌───────────────────────────────────────────┐
│  Power Automate  │   function-key auth    │        Function App  (Python v2)          │
│  A Submit        │ ─────────────────────► │  function_app.py    3 routes, thin        │
│  B Poll          │                        │        ├── print_policy.py   PURE rules   │
│                  │                        │        │      stdlib only, no I/O         │
│  D Weekly digest │   counts for looping   │        ├── sharepoint.py      adapter     │
└──────────────────┘                        │        ├── universal_print.py adapter     │
         │                                  │        └── graph_client.py / graph_auth.py│
         │ SharePoint connector             └───────┬──────────────┬──────────────┬─────┘
         │ (read-only, counts)                      │              │              │
         ▼                          ┌───────────────┘              │              │
┌──────────────────┐                ▼                              ▼              ▼
│   SharePoint     │◄──┐  ┌────────────────────────┐  ┌──────────────────┐ ┌─────────────┐
│  library = the   │   └──┤  Microsoft Graph       │  │  Azure Key Vault │ │ Application │
│  queue + audit   │      │  • list items + fields │  │  refresh token   │ │  Insights   │
│  (source of      │      │  • driveItem content   │  │  read + rotate   │ │ RUN_SUMMARY │
│   truth for NOW) │      │  • Universal Print     │  └──────────────────┘ │ PRINT_EVENT │
└──────────────────┘      └───────────┬────────────┘   ▲ managed identity  │ (truth for  │
                                      │                │ Secrets Officer   │  OVER TIME) │
                                      ▼                                    └─────────────┘
                            ┌────────────────────┐                                ▲
                            │ Physical printer   │                                │
                            │ via UP connector   │            Workbook / Dashboard ┘
                            └────────────────────┘                   (§13)
```

### 5.2 Layering (§P-2.2)

| Module | Knows about | Must not know about |
|---|---|---|
| `graph_auth.py` / `graph_client.py` | MSAL, Key Vault, HTTP, retries, paging, unauthenticated requests | SharePoint or print semantics |
| `sharepoint.py` | Graph site/list/item/driveItem URLs, column-name resolution | what a print status means |
| `universal_print.py` | Graph print URLs, upload-session mechanics, cancel | SharePoint |
| `print_policy.py` | statuses, windows, selection, transitions, message text | HTTP, Graph, any cloud SDK |
| `function_app.py` | the sequence only | any status branching whatsoever |

`print_policy.py` is **standard library only** — the constraint that keeps the unit suite offline
and sub-second, as `field_policy.py` does in the sibling project. Single source of truth:

```python
COLUMN_STATUS  = "Print_Status"     # display names; internal names resolved at runtime
COLUMN_JOB_ID  = "Print_JobId"
COLUMN_MESSAGE = "Print_Message"
COLUMN_PRINTER = "Printer_Name"

READY = "PRINT_READY"; PENDING = "PRINT_PENDING"
FAILED = "PRINT_FAILED"; COMPLETED = "PRINT_COMPLETED"

DEFAULT_BATCH_SIZE = 5;  MAX_BATCH_SIZE = 15
DEFAULT_WINDOW_DAYS = 20; DEFAULT_MIN_AGE_HOURS = 72
BUSINESS_TZ = "America/Vancouver"; MESSAGE_MAX_CHARS = 255
JOB_CONFIGURATION = {"copies": 1}   # printer defaults decide duplex/colour/paper
POLICY_VERSION = "1.0"
```

**Time discipline:** every age comparison (the give-up threshold, the retry boundaries) is done in **UTC** against
`createdDateTime`. `America/Vancouver` is used for **display only**, in the `printed on …`
message. Mixing the two is how DST bugs get in; a test asserts both behaviours.

### 5.3 State machine

```
        (upstream process sets PRINT_READY — not this app, see G4)
                            │
                            ▼
                   ┌──────────────────────────────────┐
                   │           PRINT_READY            │
                   │  Print_Time empty or past = DUE  │
                   │  Print_Time in the future = the  │
                   │    row is WAITING out its backoff│
                   │    (Poll cannot see it here)     │
                   └────────┬─────────────────────────┘
                            │  Submit: only when DUE. Claim, If-Match on eTag,
                            │  writes STATUS=PENDING, PRINTER=shareId,
                            │  clears JOBID + PRINT_TIME, keeps MESSAGE
                            ▼
              ┌──────────────────────────────┐
       ┌─────►│        PRINT_PENDING         │
       │      │  jobId set   = submitted OK  │
       │      │  jobId empty = crashed after │
       │      │      claim (Poll requeues)   │
       │      └───┬───────────┬──────────┬───┘
       │          │           │          │
       │  Submit  │    Poll   │   Poll   │
       │  failed  │ completed │ canceled │
       │  5.2–5.6 │           │ aborted  │
       │          ▼           ▼          │
       │  ┌───────────────┐ ┌────────────▼──┐
       │  │ PRINT_FAILED  │ │PRINT_COMPLETED│
       │  └───────┬───────┘ │  (terminal)   │
       │          │         └───────────────┘
       └──────────┴──  Poll: stalled past stallMinutes → cancel old job, back to
                       PRINT_READY with Print_Time = when the next retry falls due
```

**Why the claim precedes submission (deviation at R5).** A crash after the claim leaves
`PRINT_PENDING` with an empty `Print_JobId` — requeued by Poll at the next retry boundary. A crash after
submitting but before writing the status would print the document **twice** on the next run.
*A recoverable lost print beats a silent double print.* The consequence the whole design must
respect: **Poll must tolerate `PRINT_PENDING` rows with no job id and never treat them as errors.**

**Job ids are not globally unique.** Universal Print job ids are short per-printer values
— the first job this tenant's Brother ever received came back as **`"6"`**, a single digit,
measured 2026-08-30, and the next run of the same script got **`"9"`**. They are neither
globally unique nor contiguous, so `Print_JobId` can only ever be read alongside `Printer_Name`. `Print_JobId` is only meaningful together with `Printer_Name`; every lookup
uses the pair, and so does every log correlation (§13.3).

### 5.4 Column write matrix — the complete contract

`—` means the column is not touched.

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

This table **is** the Tier C test suite: one assertion per row.

**The cancel outcome is written, not assumed.** Both rows above cancel the
outstanding job first (rule 2), and the message says which of three things
happened — `cancelled`, `CANCEL FAILED`, or there being no job at all. It used to say
`cancelled` unconditionally while `_cancel_outstanding` returned the real answer
and both callers discarded it. Confirmed live on 2026-09-02: `Print_Message` read
`Job Id 38 cancelled` while Universal Print still showed job 38 as `stopped`.
Since a requeue clears `Print_JobId`, that false claim was the only surviving
record of the job.

Only `CANCEL_OK` earns the word `cancelled`; an unrecognised result under-claims,
because a needless "check the printer" costs a glance and the reverse hides a
duplicate.

A **lingering job** — the cancel accepted but the job still reporting a live state
— is reported as a `warning` on the run's response item and an `ERROR` log line,
**not** in `Print_Message`. Cancel is asynchronous, so that state is a snapshot
that may resolve itself; the column records what was done, the run records what
was seen.

**`Print_Time` is only ever set on a `PRINT_READY` row.** That invariant is why it is
cleared on the claim and on every terminal write, and it is asserted directly. Without
it a human resetting a terminal row to `PRINT_READY` — the documented recovery for
`PRINT_FAILED`, which is terminal — would inherit a stale future due time, and Submit
would silently decline to print it. The reset would appear to do nothing at all.

Two rows disappeared when the retry ladder moved into the column. *"Stalled, inside
the backoff gap"* and *"stalled, retries exhausted, inside give-up"* both used to
write nothing: the wait was a comparison Poll made privately on every run. Poll now
requeues the moment a job is stalled and records **when** the next attempt falls due,
so the waiting happens in Submit against a value anyone can read.

**`Print_Time` is a `Date and Time` column** — created 2026-09-02 with *Include
Time* **on** and *Friendly format* **off**, so it sorts as a moment and renders as
an absolute timestamp rather than "in 2 days". It is deliberately **not indexed**:
SharePoint honours one indexed field per `$filter` and `Print_Status` holds that
slot, which is why Submit compares the due time in Python (§5.5).

Because the column is typed, SharePoint parses our ISO string to an instant and
displays it in the **site's** regional settings. Writing it in the business zone is
therefore what makes the value correct *on the wire* — see §5.7 — not what the
reader sees; the two happen to be the same clock here.

**It should carry no SharePoint default value.** A default such as `Today's date`
would stamp every new row, and the column would stop meaning *"a retry is scheduled
for exactly then"*, which is the only reason it exists. This is legibility, not
correctness: a default in the past still reads as due, so the pipeline works either
way. Empty means nothing is scheduled, and `is_due` treats that as *print now*.

**The last two rows are the only places the app appends to `Print_Message`**
rather than replacing it. Everywhere else the column is overwritten wholesale.
`Print_JobId` is cleared on a requeue because the job has been cancelled and no
longer exists — but its id survives inside the appended message, so the audit
trail is not lost.

> **Why the claim PRESERVES `Print_Message`** (it used to clear it). The retry
> history is written by Poll on the way *out* of `PRINT_PENDING`; Submit's claim is
> the very next write. Clearing there erased the history within one Flow A cycle —
> at most fifteen minutes — so the column could never hold more than a single
> entry and the append was a no-op in practice.
>
> Nothing is lost by keeping it. Every terminal outcome REPLACES the column:
> `printed on …` on success, the error text on failure. So a stale entry can only
> be visible while the row is `PRINT_PENDING`, which is exactly when someone
> wondering "why is this taking so long?" wants to read it.
>
> `Print_JobId` is still cleared, and that half **is** load-bearing: a stale job id
> would send Poll looking up a job belonging to a previous attempt.

### 5.5 Submit — sequence

```
POST /api/print/submit {sharepointHostname, sharepointSitePath, library, folder,
                        printerShareId, batchSize?=5, printFormat?, dryRun?}
 ├─ 1 validate                              → 400 on any problem, ZERO writes (§P-2.4)
 ├─ 2 resolve site → list (title|id) + the 5 column internal names   [cached per list]
 │      the site comes from the REQUEST, not app settings (§5.8)
 ├─ 3 PREFLIGHT  GET /print/shares/{id}?$select=id,displayName,isAcceptingJobs,
 │                 capabilities,status&$expand=printer($select=id)
 │      reject early: share missing · not accepting jobs · contentTypes lacks the file type
 │      ALSO: a printFormat the share does not report → 400, nothing claimed
 │      cache the PRINTER id from the expand — Poll needs it to cancel (§5.7)
 ├─ 4 GET items  $filter=fields/<status> eq 'PRINT_READY'  $expand=fields
 │               Prefer: HonorNonIndexedQueriesWarningMayFailRandomly
 │               follow @odata.nextLink to a page cap
 │      then IN PYTHON: scope to folder → DROP rows whose Print_Time is still in
 │                     the future → sort by createdDateTime asc → take batchSize
 │      (client-side: $orderby on fields/* is unreliable, and only one indexed
 │       field may be filtered at a time -- Print_Status holds that slot, which is
 │       why the due-time comparison happens here and Print_Time needs no index)
 │      THE DUE FILTER RUNS BEFORE THE BATCH IS TAKEN. A repeatedly-requeued file
 │      has the OLDEST creation time, so it sorts to the front and is also the one
 │      most likely to be waiting; filtering afterwards would let waiting files
 │      fill every slot while printable ones sat behind them.
 │      A blank or unreadable Print_Time means DUE NOW (§5.4).
 └─ 5 per file, sequentially. THE BUDGET IS CHECKED BEFORE STARTING A FILE, NEVER MID-FILE,
      so the 90 s cap can never strand a claimed row:
      5.1 PATCH items/{id}/fields  If-Match: <eTag>          → CLAIM
          412 ⇒ another run won it ⇒ skipped, next file
      5.2 GET items/{id}/driveItem?$select=id,name,size,@microsoft.graph.downloadUrl
          GET <downloadUrl>          ← NO Authorization header (preauthenticated)
      5.3 select the PROFILE: by printFormat when given, else by capabilities
          POST /print/shares/{s}/jobs {"configuration": <profile's>} → jobId, documentId
          a format this DOCUMENT cannot produce → this file fails, batch continues
      5.4 POST …/documents/{d}/createUploadSession {documentName, contentType, size}
      5.5 PUT <uploadUrl>            ← NO Authorization header
          Content-Range: bytes {s}-{e}/{total} · Content-Length
          one range if < 4 MB; else sequential 200 KB multiples under 10 MB
          (20 x 200 KB). 202 per range, 201 on the last, which is asserted --
          a silent 202 on the final range means the document is incomplete.
          on abandonment → DELETE <uploadUrl>
      5.6 POST …/jobs/{jobId}/start
      5.7 PATCH fields {Print_JobId: jobId}   ← GUARDED: see below
      ──  any failure in 5.2–5.6 → PATCH {PRINT_FAILED, printer, message}

 ◄─ 200 {candidatesFound, remainingReady, submitted, failed, skipped, notYetDue,
         printerAvailable, budgetExhausted, batchSize, printFormat,
         items:[{itemId, fileName, result, jobId, message, warning?}]}

      candidatesFound and remainingReady count only what is DUE. notYetDue is
      separate and NOT folded in: Flow A runs `Do Until remainingReady = 0`, and a
      file waiting on a future Print_Time can never be submitted this cycle, so
      counting it there would spin the loop to its iteration cap every recurrence.
      Reported rather than dropped, because "nothing to print" and "nothing due
      yet" are different situations.

      printFormat is null when the caller named none and the printer's
      capabilities chose the profile -- the pre-parameter behaviour.
```

> **Step 5.7 must not be allowed to raise (S1).** By the time it runs the job is created *and*
> started — paper is on its way. An unguarded failure there did two things: it killed the batch with
> a 500, discarding the record of every file that had already printed, and it left the row at
> `PRINT_PENDING` with an **empty** `Print_JobId` — which §5.3 defines as "a crashed submission
> a crashed submission". Poll would then print the document a second time. That is the silent double
> print the claim-first ordering exists to prevent, reintroduced one line from the end. The write
> cannot be recovered, so instead the batch continues, the per-item record carries a `warning`, and
> an `ERROR` line names the coming duplicate — the only warning anyone will ever get. `PRINT_EVENT`
> still carries the job id, so the correlation survives even though the column does not.

> **`printerAvailable` is on every Submit response (S3).** When the share is not accepting jobs the
> run is a healthy `200` with `failed = 0`, matching no error condition, and `remainingReady` is
> `0` so Flow A's `Do Until` ends rather than spinning to its iteration cap. Without an explicit
> flag an offline printer would be completely silent, so Flow A's notify condition tests it.

### 5.6 Poll — sequence

> **Revised twice.** After review (F1), Poll stopped taking a batch of 15: that
> violated the requirement ("query ... for **all** files") and starved, because a
> handful of long-running jobs held every slot run after run. Then on 2026-09-01
> it absorbed Resubmit entirely, so it now owns the full lifecycle of a
> `PRINT_PENDING` row.

```
POST /api/print/status {sharepointHostname, sharepointSitePath, library, folder,
                        printerShareId?, giveUpDays?=10, stallMinutes?=5}
 ├─ validate → resolve list + columns
 ├─ GET items $filter=fields/<status> eq 'PRINT_PENDING'
 │     NO window pre-filter. Every pending row reaches the loop -- an old one is
 │     now FAILED explicitly rather than dropped out of scope (this is what
 │     closes G1). No batch cap either; only the wall-clock budget bounds it.
 │     SKIP items with a Print_JobId and no printer at all → malformed (G3).
 │     An override SUPPLIES that printer, so it rescues those rows.
 └─ per item:
      printer = printerShareId OR Printer_Name    ← HARD override when supplied
                a row disagreeing with the override is counted as
                printerOverridden and logged WARNING (F3-R)
      job = Print_JobId ? GET /print/shares/{printer}/jobs/{id} : none
            404 → counted as notFound, then treated as "no job"

      poll_decision(state, file age, job age, attempt age) → in ORDER:
        1. completed            → PRINT_COMPLETED + "printed on <acknowledged>"
                                  BEFORE the give-up test: a job that finished a
                                  minute late still put paper in the tray
        2. canceled | aborted   → PRINT_FAILED + description/details
        3. past giveUpDays      → ***CANCEL***, then PRINT_FAILED + reason
        4. stalled              → ***CANCEL***, then PRINT_READY + appended note
                                  + Print_Time = when retry (spent+1) falls due,
                                    CLAMPED to created + giveUpDays
        5. otherwise            → write NOTHING (a healthy job in flight)

      Check 4 used to carry two more conditions -- "retries left" and "a NEW
      boundary crossed" -- and a row failing either waited at PRINT_PENDING while
      every run re-derived the same silence. The waiting is Print_Time's job now,
      so a stalled row leaves PRINT_PENDING at once and waits where it can be seen.

      "stalled" = no job at all (crashed submission or 404), or a live
      non-terminal job whose own createdDateTime is older than stallMinutes.
      A job whose age cannot be determined is NOT stalled -- cancelling one that
      might be printing would queue a second copy.

 ◄─ 200 {checked, completed, failed, requeued, gaveUp, stillRunning, notFound,
         malformed, pendingFound, uncheckedCount, budgetExhausted,
         giveUpDays, stallMinutes, printerShareId,
         printerOverridden, items:[…]}
      Each requeued item also carries `retry` and `printTime`, so a run says
      which attempt it scheduled and for when.

      printerOverridden is NOT an error count. It counts CONFIGURATION
      divergence: every row whose Printer_Name disagreed with the override,
      whether or not it has a job. Only the rows that DO have an outstanding job
      carry the F3-R double-print risk, and only those get the F3 warning in the
      log. It is 0 in a single-printer deployment.

      Two of those account for every row exactly once:
          checked + uncheckedCount == pendingFound
      A crashed submission (PRINT_PENDING, no job id) lands in `checked` like
      anything else, because Poll now acts on it. It used to be counted nowhere,
      so the one state the design tells you to expect was the one the response
      could not show (S5).

      The three knobs are echoed back so the response says what was actually in
      force, not what the flow believed it sent.
```

### 5.7 The retry schedule

Retry `n` falls due at file age **`stallMinutes × (2ⁿ − 1)`**. With the defaults:

| Retry | Due at | | Retry | Due at |
|---|---|---|---|---|
| 1 | 5 min | | 7 | 10h 35m |
| 2 | 15 min | | 8 | 21h 15m |
| 3 | 35 min | | 9 | 1d 18h 35m |
| 4 | 1h 15m | | 10 | 3d 13h 15m |
| 5 | 2h 35m | | 11 | 7d 2h 35m |
| 6 | 5h 15m | | 12 | *14d 5h — clamped to 10d* |

**The count is derived, never stored.** There is no attempt counter. Because every
requeue creates a *new* print job, the current job's creation time says how far
into the schedule this attempt began:

> `spent` = `retries_due(the file's age when this attempt started)`.
> A stalled job is requeued at once, scheduling retry `spent + 1` for
> `min(created + stallMinutes × (2^(spent+1) − 1), created + giveUpDays)`.

```
T+0    file created, job J1        spent 0  -> REQUEUE, Print_Time = T+5  (past)
T+10   Submit creates J2           spent 1  (retries_due(10) = 1)
T+15   J2 stalled                  spent 1  -> REQUEUE, Print_Time = T+15 (now)
T+20   Submit creates J3           spent 2
T+25   J3 stalled                  spent 2  -> REQUEUE, Print_Time = T+35 (FUTURE)
       ...row sits at PRINT_READY, Submit declines it...
T+35   Submit creates J4                     the wait was a value, not a decision
```

**The wait moved from Poll to Submit.** Poll used to hold a stalled row at
`PRINT_PENDING`, re-deriving the same "not yet" on every run; the backoff was a
comparison nobody could see. Now the row is requeued immediately and carries the
due time with it, and Submit is what honours it. Same ladder, same instants —
written down. The offline printer is still guarded: the expensive work (render,
conversion, upload) happens when Submit acts, and Submit will not act early.

> **A due time in the past means "print now".** The early boundaries have usually
> gone by before Poll first notices a stall, so the first requeues are effectively
> immediate — which is what reproduces the pre-`Print_Time` timing.
>
> **The quantisation moved from Flow B to Flow A.** The old note here said retry 2
> was "consumed by Flow B's cadence". Half of that is fixed and half of it simply
> changed hands, and the difference matters when reading a live run.
>
> *Fixed:* once a row is requeued, `Print_Time` pins the exact instant and Submit
> acts at the next Flow A tick at or after it, so no mid-schedule boundary is lost
> to the polling interval any more. Simulated against the real `poll_decision`,
> Flow B at 10 minutes and at 1 minute produce **identical** retry sequences.
>
> *Changed hands:* the **front** of the ladder is still skipped, and now it is
> **Flow A's** recurrence that decides how much. `spent` is read off the file's age
> when the *first* job is created, and a new file is not submitted the instant it
> appears — so a file first claimed at 15 minutes already counts two retries as
> spent, and its schedule opens at retry 3.
>
> | Flow A recurrence | first job at | schedule opens at |
> |---|---|---|
> | 1 min | 1 min | retry 1 |
> | 5 min | 5 min | retry 2 |
> | 15 min | 15 min | retry 3 |
> | 30 min | 30 min | retry 3 |
>
> **Lowering `stallMinutes` does not fix this and can make it worse.** It
> compresses the ladder, so more of it falls behind the first submission: with
> Flow A at 15 minutes, `stallMinutes = 1` opens at retry **5** rather than retry 3.
> Shortening Flow A's recurrence is the only thing that recovers the early retries.
>
> None of this changes the outer bound. Every configuration above gives up at
> **10.00 days**, because that is a property of `giveUpDays` and the clamp, not of
> either cadence.

**One bound, not two.** `giveUpDays` fails the row at 10 days and is the only
limit. `maxRetries` used to bound the *work* separately, stopping new jobs at about
3d 13h and leaving a grace period until day 10 — but the retry count is derived
from the file's age anyway, so a count and a deadline were two answers to one
question. With the cap gone the ladder reaches retry 11 at 7.11 days, and retry 12
(14.22 days) is **clamped to the deadline**.

> **Why the clamp is load-bearing.** A row waiting on a future `Print_Time` sits at
> `PRINT_READY`, and Poll queries `PRINT_PENDING` only — so a retry scheduled past
> the deadline would be invisible at the exact moment the row was due to be failed,
> and would print days late against a row that then reads `PRINT_FAILED`. Clamped,
> the last attempt lands *on* the deadline: it gets `stallMinutes` of life, and the
> next run finds the file out of window and fails it.
>
> **The grace period is gone with it.** Work now continues to the cutoff: twelve
> renders for an undeliverable document instead of eleven. And because a requeue
> cancels the outstanding job (rule 2), a row that is *waiting* has no live job —
> so a printer fixed mid-wait no longer prints immediately, as it did when the last
> job was left alive. Expect that during a live stall test; it looks like nothing
> is happening.

**Unbounded, and safely so.** `retries_due` no longer takes a cap. The ladder
doubles, so the loop is logarithmic — 32 iterations for a year-9999 timestamp at a
one-minute base — and `poll_decision` only consults it after `within_window` has
passed, which bounds the file's age at `giveUpDays`. Swept over every legal pair of
knobs, it returns at most **19** (`stallMinutes` 1, `giveUpDays` 365), so the
highest retry number ever scheduled is **20**.

> **Why cancel first.** `stopped` means "an issue with the printer needs to be
> addressed **before the job can continue**" — the job is alive. Requeue without
> cancelling and, when the printer is fixed, the original *and* the replacement
> both print. Cancel is best-effort: on failure we still requeue, because a stuck
> document is the worse outcome — but we log it, and that log line is what tells
> you a duplicate is possible.
>
> **Giving up cancels too.** Otherwise an abandoned job prints days later against
> a row that reads `PRINT_FAILED`, and the column is lying about paper.
>
> Cancel is documented only on `/print/printers/{id}/jobs/{id}/cancel`, so it needs
> the PRINTER id resolved from the share named in `Printer_Name`.

### 5.8 Tuning without a deploy

`batchSize`, `giveUpDays` and `stallMinutes` resolve **request body → default**.
The request body is how Power Automate sets them, so the pacing of the whole retry
schedule is a number in a flow, not a redeploy. An out-of-range *request* value is
a 400.

**The app-setting layer is gone, deliberately.** Each of these used to fall back to
a `PRINT_*` setting, on the reasoning that a server should be able to retune itself.
In practice it split the answer to *"why is the pacing wrong?"* across a flow and an
app setting, with the flow silently winning — so a setting could be read, believed,
and be doing nothing at all. A leftover value in the environment now changes
nothing, which is what makes the deploy sequence in §5.10 safe: the settings can be
deleted after the flows are confirmed green rather than before.

`PRINT_BUDGET_SECONDS` and `PRINT_BUSINESS_TZ` keep their settings on purpose.
Neither is something a flow sets: one is a property of the host's connector
timeout, the other of the office reading the library.

**The site is request input too.** `sharepointHostname` and `sharepointSitePath`
were `SHAREPOINT_HOSTNAME` / `SHAREPOINT_SITE_PATH` in app settings. One Function
App now serves any site a flow names, and everything that shaped a run is visible in
the flow that made it. The trade is real and is recorded in CLAUDE.md: whoever holds
the function key chooses the site, bounded only by what the delegated service
account can reach. A missing value is a **400**, not the 500 the old `RuntimeError`
produced — it is the caller's error, and is now answered as one.

### 5.9 Health — sequence

Numbered 5.9 rather than inserted after Poll because §5.7 is cross-referenced
from §4 and §5.5; renumbering would silently break both.

```
POST /api/print/health {printerShareId, printFormat?}
 ├─ 1 validate                        → 400 on any problem, ZERO Graph calls
 │      an unknown printFormat is a 400 -- a malformed REQUEST is not an
 │      unhealthy PRINTER, and answering 200/unhealthy would send somebody to
 │      check a device over their own typo
 ├─ 2 GET /print/shares/{id}?$select=…&$expand=printer($select=id)   ONE call
 │      AuthBootstrapRequired → AUTH_BOOTSTRAP_REQUIRED ┐ terminal: one error,
 │      GraphError 404        → PRINTER_NOT_FOUND       │ printer/conversion
 │      GraphError otherwise  → PRINTER_UNREACHABLE     ┘ null, stop here
 └─ 3 accumulate EVERY remaining finding -- never stop at the first, or a flow
      learns one problem per cycle and takes an hour to hear six
 ◄─ 200 {healthy, printerShareId, printFormat, printer, conversion,
         errors[], warnings[], message}
```

**Always 200 when the check ran.** 400 = malformed request, 500 = Health itself
broke; a non-200 never means "the printer is sick". Power Automate marks a
non-2xx HTTP action as *failed*, which halts the branch unless every downstream
action carries a run-after override — and the body, which is the whole diagnosis,
becomes awkward to read exactly when it matters.

`healthy`, `errors` and `warnings` are on **every** 200, so a flow condition
needs no null check. That is the S3 lesson applied up front rather than after.

| Errors (`healthy: false`) | Warnings (still healthy) |
|---|---|
| `AUTH_BOOTSTRAP_REQUIRED` · `PRINTER_NOT_FOUND` · `PRINTER_UNREACHABLE` · `PRINTER_NOT_ACCEPTING_JOBS` · `PRINTER_STOPPED` · `NO_PRINTER_ID` · `FORMAT_NOT_SUPPORTED` · `NO_PROFILE` · `JOB_CONFIGURATION_FAILED` · `CONVERTER_UNAVAILABLE` | `NO_CONTENT_TYPES` · `PRINTER_STATE_UNKNOWN` |

The codes are a **closed, stable vocabulary** — a flow condition and a KQL query
both key on them, so renaming one is a breaking change no Python would catch.
`print_policy.ALL_HEALTH_CODES` is the list, and a test asserts they are unique.

Three of these are what Health is *for* — the app either misses them entirely or
notices far too late:

- `NO_PRINTER_ID` — cancel is documented only on the printer route, so a share
  with no printer id means Poll cannot cancel a stalled job before requeuing it,
  and the original prints alongside its replacement (rule 2, D1). `cancel_job`
  **does** notice, but only when the cancel is attempted: after a job has stalled,
  as a `WARNING` line, with the duplicate already unavoidable.
- `CONVERTER_UNAVAILABLE` — `pwg_converter` imports `pypdfium2` lazily inside
  `convert_pdf`, so a wheel that failed to install is otherwise found one
  document at a time, *after* each has been claimed.
- `PRINTER_STOPPED` — see the `status.state` row in §4.

`conversion` comes from the **same function the dry run uses**, so Health cannot
describe a pipeline Submit would not run; `printer` comes from the same
`_printer_report`. Both are pinned by parity tests against `dryRun`.

---

## 6. Cross-cutting design

**6.1 Column-name resolution.** `GET /sites/{s}/lists/{list}?expand=columns(select=name,displayName)`
→ `displayName → name`, cached per list. This is what makes an encoded internal name a non-issue.
An unresolvable column is a **500 naming it** — silently skipping would leave statuses unwritten forever.

**6.2 Retry and throttling.** Retry `429/503/504` honouring `Retry-After`; max 3; exponential
backoff with jitter. Never other 4xx. **The create-job `POST` is attempted once** — a retry risks a
duplicate job. Log `X-MSEdge-Ref` and `request-id` from print responses.

**6.3 Timeouts (§P-2.6).** 30 s per Graph call (`GRAPH_TIMEOUT_SECONDS`, resolved per call in
`graph_client.resolve_timeout_seconds`); a 90 s whole-invocation budget undercutting Power
Automate's ~120 s connector budget, checked **before starting each file** so the endpoint stops
cleanly with `budgetExhausted: true` rather than being killed mid-write.

The budget is anchored at the moment the **request arrives**, not at the moment the file loop
begins. Site resolution, the printer preflight and the status query all run first and can be slow;
timing only the loop made that work free, so a run could still overshoot the connector budget — and
a connector that has already given up never receives `remainingReady`, so the flow neither loops nor
notifies. Pinned by `test_the_budget_covers_the_whole_invocation_not_just_the_file_loop`.

**6.4 Auth.** In-process access-token cache, refreshed within 5 min of expiry; the rotated refresh
token written back to Key Vault every time. No lease (§4 correction 2). A revoked token surfaces
as a 500 naming `scripts/bootstrap_token.py`.

**6.5 Configuration (§P-2.7).** Three kinds now, not two.

**Values that name the AZURE environment** get no default and raise when missing:
`GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `KEY_VAULT_URI`. These are properties of the
deployment and stay in app settings.

**Values that name the SHAREPOINT environment** are **request input**:
`sharepointHostname` and `sharepointSitePath`. They were `SHAREPOINT_HOSTNAME` /
`SHAREPOINT_SITE_PATH`; a missing one is a **400**, not the 500 the old
`RuntimeError` produced. See §5.8 for the trade this makes.

**Flow-facing tunables** get a constant default + **range validation**, and no env
override at all: `batchSize`, `giveUpDays`, `stallMinutes`. Out-of-range **request**
→ 400. A leftover `PRINT_BATCH_SIZE`, `PRINT_GIVE_UP_DAYS`, `PRINT_STALL_MINUTES` or
`PRINT_MAX_RETRIES` in app settings now does nothing whatsoever.

**Host tunables** keep the constant + env override, because no flow sets them:
`PRINT_BUSINESS_TZ`, `PRINT_BUDGET_SECONDS`, `GRAPH_TIMEOUT_SECONDS`,
`PRINT_RASTER_DPI`, `PRINT_RASTER_MAX_BYTES`. An out-of-range value there warns and
falls back, because server misconfiguration must not fail every request.

**Two request-only inputs have no env fallback**, because neither is a threshold
that could sensibly have a server-wide value:

| Input | Endpoint | Absent means |
|---|---|---|
| `printFormat` | Submit | choose the profile from the printer's capabilities — the pre-parameter behaviour |
| `printerShareId` | Poll | each row follows its own `Printer_Name` — the pre-parameter behaviour |

Both are **opt-in**: a flow that omits them gets exactly what it got before they
existed, which is what let them ship without touching Flow A or Flow B.

`GRAPH_TIMEOUT_SECONDS` [1, 300] lives in `graph_client`, not `print_policy`: a socket timeout is
transport configuration, not a business rule, and `graph_client` must keep importing no domain
(§5.2). It is read **per call** rather than captured at import, so a settings change takes effect
on the next invocation instead of the next cold start — and it applies to the unauthenticated
download and upload paths too, which would otherwise have been a half-setting. It shipped in the
template and in the deploy runbook for a while while **no code read it at all**, which is the worst
kind of configuration: setting it appeared to work and did nothing (S4).

**6.6 Prerequisites.**
- **Index the `Print_Status` column** — non-indexed columns cannot be used in `$filter` at all.
- ~~Confirm whether content approval is enabled on the library.~~ **Checked
  2026-08-31: off**, and require-check-out is off too. No moderation handling needed.
- Entra app: public client flows enabled; delegated `PrintJob.ReadWriteBasic`, `PrintJob.Create`,
  `Printer.Read.All`, `PrinterShare.ReadBasic.All`, `Sites.ReadWrite.All`, `offline_access`;
  admin consent. (`PrintJob.ReadWriteBasic` covers cancel as well as create/start;
  `createUploadSession` accepts only `PrintJob.Create` or `PrintJob.ReadWrite`, which is
  why both are listed — see `graph_auth.SCOPES` for the per-call table.)
- Function App managed identity → **Key Vault Secrets Officer**.

---

## 7. Use cases

| ID | Scenario | Expected behaviour |
|---|---|---|
| **UC-1** | 12 files `PRINT_READY`. Flow A runs. | Call 1 submits 5, `remainingReady: 7`. Calls 2–3 submit 5 and 2. Call 4 returns 0 and the loop ends. |
| **UC-2** | Printer share offline / not accepting jobs. | Preflight fails **before any claim**. 200, `submitted: 0`, clear message, **no column touched**. Files stay `PRINT_READY`. |
| **UC-3** | Unsupported file type. | Caught at preflight against `capabilities.contentTypes`; that file gets `PRINT_FAILED` + explicit message, not an opaque upload error. |
| **UC-4** | Job prints normally. Flow B runs. | `PRINT_COMPLETED`, `Print_Message = "printed on 2026-08-30 14:23:23"` (Vancouver). Terminal. |
| **UC-5** | Someone cancels at the printer. | Poll sees `canceled` → `PRINT_FAILED` + description. **Terminal** — a deliberate cancel is respected, not undone. |
| **UC-6** | Crash between claim and job creation. | `PRINT_PENDING`, **empty** `Print_JobId`. Poll reads that as stalled and requeues it at the next Poll run, ≈10 min. No double print, no lost file. |
| **UC-7** | Flow A schedules overlap; two runs start together. | Both pick the same files; `If-Match` means exactly one wins per file, the loser records `skipped`. **No file printed twice.** |
| **UC-8** | Printer unplugged; job sits `stopped` for 4 days. | Poll **cancels the stopped job** and requeues on the backoff, the last requeue at ~3d 13h, then holds. When the printer is fixed the old jobs are gone, so only the newest prints. |
| **UC-9** | Job completes but Universal Print purges it before Poll runs. | Poll gets 404, reads it as stalled and requeues → **prints twice.** Unchanged in likelihood — the risk is set by Poll's 10-minute cadence against the retention window (§3) — but the reprint now lands in minutes, not days. |
| **UC-10** | `PRINT_PENDING`, 25 days old. | **FIXED.** Past `giveUpDays` Poll cancels the outstanding job and writes `PRINT_FAILED` with a reason. Nothing strands (**G1** closed). |
| **UC-11** | Malformed request. | 400 with a specific message and **zero** SharePoint writes, so the corrected retry is not locked out. |
| **UC-12** | Refresh token revoked (service account password reset). | Every endpoint 500s with an unambiguous "re-run `scripts/bootstrap_token.py`"; no partial writes; Flow C's notification fires. |
| **UC-13** | "How many printed last week?" | §13.5 query 2, or the workbook's weekly chart. |

---

## 8. Repo layout (§P-2.1)

```
noble-print/
├─ functionapp/                     # the ONLY thing that ships
│  ├─ function_app.py               # 3 routes; thin orchestration, no status branching
│  ├─ print_policy.py               # PURE rules — stdlib only (§5.2)
│  ├─ graph_auth.py  graph_client.py
│  ├─ sharepoint.py  universal_print.py
│  ├─ host.json                     # SAMPLING DISABLED — see §13.4
│  ├─ requirements.txt  .funcignore
│  └─ local.settings.json.template  # TRACKED, secrets blank; real file gitignored
├─ scripts/                         # NOT deployed
│  ├─ bootstrap_token.py            # one-time device-code sign-in → seeds the KV secret
│  ├─ test.py                       # ONE harness; --base-url defaults to localhost (§P-6.5)
│  ├─ start-local.ps1  localshim/sitecustomize.py
├─ tests/  conftest.py  fake_graph.py  fixtures/*.json  test_*.py
├─ docs/design.md  docs/timing.md  docs/e2e-testing.md  docs/deploy-to-azure.md  docs/ai/
└─ CLAUDE.md  README.md  pytest.ini  .gitignore
```

---

## 9. Designed for reuse (requirement scalability, not traffic)

Reuse comes from **where things live**, so a new requirement is a small edit in one known place.

| A future change | What it costs |
|---|---|
| A new status value, or renaming one | One constant in `print_policy.py` + its row in the transition table |
| Different column display names | Four constants. Internal names resolve at runtime, so nothing else changes |
| Different windows or batch size | Already request parameters with env defaults |
| Duplex / colour / copies | One dict, `JOB_CONFIGURATION`, passed straight through |
| A fourth endpoint (e.g. Cancel) | A new route + one policy function. Adapters unchanged |
| Print from somewhere other than SharePoint | Replace `sharepoint.py`. Policy and print adapter untouched |
| Print to something other than Universal Print | Replace `universal_print.py`. Nothing else moves |
| A second workflow with its own vocabulary | Import the adapters directly — they know nothing about print statuses — and write its own policy module |

Two structural rules keep this true, **both enforced by tests**:
- `print_policy.py` imports nothing but the standard library.
- `function_app.py` contains no status branching.

Deliberately **not** built: profile/config system, per-file print settings, multi-library fan-out,
extra filter predicates. Each is cheap to add given the layering; building them now would be
configurability nobody requested (§P-2).

---

## 10. Testing

**Tier A — pure rules, `tests/test_print_policy.py`** (stdlib only, no network, sub-second)
- Retry boundaries pinned literally (5, 15, 35, 75, 155, 315, 635, 1275, 2555, 5115 min); the
  give-up edge across a Vancouver DST
  transition — **asserting the window is computed in UTC while the message renders local**
- oldest-first selection ascending, deterministic on ties; exactly `batchSize` taken
- all **8** job states map to the right action, including `unknown` and the cancel-first set
- `"printed on YYYY-MM-DD HH:MM:SS"` formatting, incl. a UTC instant crossing a local day
- message truncation; a failure message is never empty
- config parsing: request out-of-range → error; env out-of-range → warn and fall back
- **structural:** `print_policy` imports only the standard library (guards §9)

**Tier B — adapters against `tests/fake_graph.py`.** A `FakeGraph` implementing the exact URL
surface, recording every request **including headers**, programmable to fail a chosen call — the
analogue of `FakeTable` in the sibling repo, so **the real adapter code runs against it**.
- column resolution handles an encoded internal name; a missing column raises
- the emitted `$filter` and `Prefer` header are exact; `@odata.nextLink` paging is followed
- **the download GET and the upload PUT carry no `Authorization` header** — a direct regression
  test for the documented 401
- chunking: 1 KB → one `PUT` ending `201`; 9 MB → 200 KB-multiple ranges under 10 MB driven by
  `nextExpectedRanges`, `202`…`202`, `201`
- preflight rejects a share not accepting jobs, or lacking the file's content type, **and captures
  the printer id from the `printer` expand**
- cancel targets the **printers** path with that printer id and accepts `204`
- `429` + `Retry-After` retried; `403` not; the create-job POST never retried

**Tier C — route tests, one per endpoint.** Handlers invoked offline via the v2 model
(`function_app.submit_print_jobs.build().get_user_function()`), with `FakeGraph` and a stubbed
token provider. **The §5.4 write matrix is the specification: one test per row.**
- Submit writes claim-then-jobId, and the **call order is asserted**
- a failure at each of download / create / upload / start → `PRINT_FAILED` + printer + non-empty
  message, and **no** `Print_JobId`
- `412` on claim → `skipped`, **no print job created** (UC-7)
- bad request → 400 with **zero** SharePoint writes (UC-11)
- budget exhaustion stops **between** files, never leaving a claimed-but-unsubmitted row
- Poll — completion and failure: `completed` → exact regex, and it wins even past the give-up
  deadline; `canceled`/`aborted` → `PRINT_FAILED`; a job inside `stallMinutes` writes nothing;
  empty `Printer_Name` reported as malformed
- Poll — the retry schedule: every boundary pinned literally (5/15/35/…/5115 min), and
  `next_retry_time` agreeing with `retries_due` at each one; a stalled job is **requeued at once
  with the next boundary written into `Print_Time`**, clamped to the give-up deadline; a crashed
  submission (empty `Print_JobId`, UC-6) and a 404 are both requeued; a job whose age cannot be
  determined is left alone
- Poll — **a stalled job is CANCELLED before the requeue** (D1/UC-8), the cancel uses the PRINTER id
  from the row's own `Printer_Name`, and a failed cancel still requeues but is logged
- Poll — giving up: past `giveUpDays` the outstanding job is cancelled and the row written
  `PRINT_FAILED` with a reason; the clamped final attempt lands ON the deadline and the run after
  it fails the row rather than leaving it stranded at `PRINT_READY`
- Poll — tunables: `giveUpDays`/`stallMinutes` honoured from the request body, echoed
  in the response, and an out-of-range one is a 400 that writes nothing
- auth: live token reused; near-expiry triggers exactly one refresh; rotated token written back;
  a revoked token surfaces the bootstrap message (UC-12)
- **logging:** every terminal per-file outcome emits exactly one `PRINT_EVENT` with the documented
  field order, and every invocation emits exactly one `RUN_SUMMARY` — the reporting in §13 is only
  as good as this, so it is asserted, not assumed

### 13.11 Two additions made during implementation

- **`"dryRun": true` on Submit.** Resolves the site, library, the four internal
  column names, and the printer's capabilities and printer id, lists what a real
  run would take, and writes nothing. Deliberately a mode of the ENDPOINT rather
  than logic in the harness: it therefore exercises the same auth, resolution and
  queries the real run uses, so a green dry run is evidence about the deployed
  app rather than about a script.
- ~~**`staleCount` / `staleItems` on Poll.**~~ **Removed 2026-09-01.** They
  reported the files past the 20-day window that nothing would ever touch again
  (G1). That hole is now fixed rather than reported: Poll examines every pending
  row and writes `PRINT_FAILED` with a reason past `giveUpDays`, so there is
  nothing left to strand. Leaving the fields in would report zero forever and
  imply the cliff was still there.

**Tier D — one live harness, `scripts/test.py`** (stdlib only; `pytest.ini` sets `testpaths = tests`).
`--base-url` defaults to `http://localhost:7071`, `--key` for the deployed app (§P-6.5).
Subcommands `submit` / `status`, plus `dryrun` (resolve site, list, **the four
internal column names**, index and content-approval state, printer capabilities and printer id —
print them, print nothing on paper) and `badpayload` (must 400 with no writes). The retry knobs
`--stall-minutes` / `--give-up-days`, and the required `--hostname` / `--site-path`, go into the body exactly as a flow sends
them, so a value can be proved here before it is pasted into Power Automate.

**Tier E — deploy smoke sequence** (§P-7.6): bad payload → 400 · `--dry-run` → resolved names ·
one real file → a job id · **then read the four SharePoint columns back** · **then run §13.5 query
1 and confirm the `PRINT_EVENT` row is in App Insights.** The last two are the steps people skip.

---

## 11. Verification

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r functionapp\requirements.txt
.\.venv\Scripts\python.exe -m pytest                       # Tiers A-C, offline, no cloud account

.\.venv\Scripts\python.exe scripts\bootstrap_token.py      # one-time device-code sign-in
.\scripts\start-local.ps1                                  # venv + TLS shim + func start
.\.venv\Scripts\python.exe scripts\test.py --dry-run --library "Documents" --folder "/Invoices/ToPrint"
.\.venv\Scripts\python.exe scripts\test.py submit --hostname "<tenant>.sharepoint.com" --site-path "/sites/<site>" --library "Documents" --folder "/Invoices/ToPrint" --printer-share-id "<guid>" --batch-size 1
```

Then open the library and confirm the five columns match §5.4.

## 12. Deploy order (§P-7.1)

> The executable version of this list — every command, in order, with the failure
> modes — is **[deploy-to-azure.md](deploy-to-azure.md)**. This section is the
> summary; that document is what you actually follow. Keep them in step, or
> delete this one rather than let two copies drift.

1. Entra app registration + admin consent
2. **Index `Print_Status`** in the SharePoint library
3. Key Vault, `Secrets Officer` for the app identity, `bootstrap_token.py` to seed the secret
4. Application settings — then **read them back** (a code default is not the live value, §P-7.2)
5. Function code (`--build remote`) — **with sampling disabled in `host.json`** (§13.4)
6. Power Automate flows A–D; Poll's cadence from the measured retention window (§3)
7. App Insights workbook + alerts (§13)

---

## 12a. Findings from the post-implementation review

Six defects were found by reviewing the finished code against the requirement.
Each had a regression test in `tests/test_review_findings.py` that failed before
the fix. Recorded here because the reasoning is worth more than the diff.

> **F2 and F6 are history, not live behaviour.** Both were properties of the
> Resubmit endpoint's shape, and both became *structurally impossible* when it was
> retired: Poll has no batch cap (F2) and runs one query (F6). Their tests went
> with the endpoint; the reasons are kept at the head of
> `test_review_findings.py` so a deleted regression cannot quietly come back.
> **D1 — cancel before replace — did NOT go away**, and its tests were ported into
> `test_poll.py` before `test_resubmit.py` was deleted.
>
> **F3 is live again, by decision.** It was structurally impossible for exactly as
> long as Poll took no printer. Poll now accepts an optional `printerShareId` that
> hard-overrides each row's `Printer_Name`, which is the precondition F3 turns on:
> job ids are per-printer, so an override naming the wrong share cannot cancel the
> row's job, and the original prints beside its replacement. The exposure is zero
> while one printer is registered — the override then equals `Printer_Name` on
> every row — and is counted as `printerOverridden` plus a WARNING when it is not.
> Recorded as **F3-R** in `docs/ai/open-defects.md` with the two fix shapes that
> were available.

| # | Defect | Why it mattered |
|---|---|---|
| **F1** | Poll capped at 15 jobs and took the oldest first | Requirement violation ("all files"), **and** starvation: long-running jobs held every slot, so newer completed jobs were never marked and aged into the 20-day dead zone |
| **F2** | Resubmit ordered by `createdDateTime` | That value never changes, so a chronically failing file was re-picked every run and everything newer waited behind it forever |
| **F3** | Resubmit cancelled on the **wrong printer** when the request overrode the printer | The cancel 404'd, a 404 reads as "already gone", so it reported success while the original job stayed alive — the exact double-print the cancel exists to prevent |
| **F4** | `FakeGraph` dropped `$filter` from its `@odata.nextLink` | A flaw in the *test harness*: real Graph carries the query forward, so no paging test could have caught a real filter-loss bug |
| **F5** | Poll stamped every completion with the request's start time | With the cap removed a run can span 90 s, so forty jobs would share one timestamp up to a minute and a half stale |
| **F6** | Resubmit could process one file twice | Its two status queries run at different instants; a file whose status changes between them appeared in both result sets |

**Requirements conformance** is now asserted directly, clause by clause, in
`tests/test_requirements.py` (26 tests) — including the places the implementation
deliberately departs from the literal text (R3, R5, R6). R12–R17 described the
retired Resubmit endpoint; only R16 survives, and it is pinned harder than before
because the retry schedule depends on it.

## 13. Reporting and observability

> Answers "how many were submitted / retried / pending / failed / completed each week", and
> "what can I use in the Azure portal".

### 13.1 The key distinction — two different questions, two different stores

| Question | Answered by | Why |
|---|---|---|
| **"What is the state *right now*?"** — how many files are pending / failed / completed today | **SharePoint** (the library itself) | The five columns *are* the state. Exact, live, zero code |
| **"What *happened* over time?"** — how many were submitted, retried, or completed each week | **Application Insights** | The columns hold only the **latest** state. A file retried three times looks identical to one retried once. Only an event log can count activity |

This is why both halves are needed. Reporting weekly counts off SharePoint alone is impossible;
reporting current state off logs alone is unreliable (a log gap becomes a wrong total).

### 13.2 Telemetry map

| Store | Holds | Questions it answers |
|---|---|---|
| **SharePoint library** | the five columns per file | "What is the business state of file X?" — the source of truth |
| **Application Insights** | `RUN_SUMMARY` (per invocation), `PRINT_EVENT` (per file transition), `requests`, `exceptions` | "How many, how often, how long, what failed" |
| **Power Automate run history** | each flow run and its retries (28 days) | "Did the schedule actually fire?" |
| **Universal Print / printer** | job queue and printer status | "Is the printer itself healthy?" |

**Health's `RUN_SUMMARY` reuses the existing fields rather than adding any**, so
the KQL `parse` stays valid for all three endpoints:

| Field | On `ep=health` |
|---|---|
| `printer` | the share id that was checked |
| `ok` | `1` when healthy, `0` when not |
| `failed` | number of **errors** |
| `skipped` | number of **warnings** |
| `lib` `folder` `found` `remaining` | sentinels — Health reads no library |

**Health emits no `PRINT_EVENT`.** That line is one per *file* per outcome and
Health touches no files, so its absence is correct rather than an oversight; a
test pins it.

### 13.3 The two log lines

Fields are embedded in the message text because the Python worker does not map `extra=` into
`customDimensions` (§P-2.8). **Free-text fields go last** so KQL `parse` delimiters stay unambiguous.

```
RUN_SUMMARY  ep=submit lib=Documents printer=<shareId> found=23 ok=4 failed=1
             skipped=0 remaining=18 httpStatus=200 ms=8421 folder=/Invoices/ToPrint

PRINT_EVENT  ep=submit item=42 from=PRINT_READY to=PRINT_PENDING job=1825
             printer=<shareId> result=submitted ms=1420 file=Invoice 2026-08 Acme.pdf
```

> **`found=` on Submit now counts only what is DUE.** The line's shape is unchanged
> — no field was added, because free text must stay last or the KQL `parse` breaks —
> but its meaning narrowed when `Print_Time` arrived: a `PRINT_READY` row waiting on
> a future retry time is no longer counted here. Expect `found` to drop for any
> library with files in backoff, and read `notYetDue` in the response (not the log)
> for the difference. A workbook comparing `found` across the change will show a
> step that is not a fall in volume.

`result` vocabulary — a closed set, so the counts are exhaustive:
`submitted · failed · skipped · completed · failed_terminal · requeued · gave_up ·
not_found · cancelled`

Nine values, and every one of them is emitted somewhere — checked by grep, not assumed. Note what
is **absent**: `still_running`. A job still in flight is not an outcome, it is the absence of one,
and emitting a line every ten minutes for every unfinished job would swamp the very table the
weekly counts are computed from. It appears in the Poll *response* (`stillRunning`), which is where
a caller wants it, and nowhere in the log.

One `PRINT_EVENT` per file per terminal outcome, from every endpoint. `item` + (`printer`,`job`)
are the correlation keys — remember job ids are only unique per printer (§5.3).

### 13.4 host.json — sampling **must** be disabled

```json
{ "version": "2.0",
  "logging": { "applicationInsights": { "samplingSettings": { "isEnabled": false } } } }
```
Adaptive sampling silently drops `traces` rows — which would make every count in §13.5 quietly
wrong, with no error anywhere. At this volume it saves nothing. The sibling project learned this
the same way.

### 13.5 KQL query pack — App Insights → **Logs**

> Table name: `traces` when querying from the Application Insights resource's Logs blade;
> `AppTraces` from the Log Analytics workspace. Same data.

**Query 1 — per-file event feed (the workhorse)**
```kusto
traces
| where timestamp > ago(7d) and message startswith "PRINT_EVENT"
| parse message with "PRINT_EVENT ep=" ep " item=" item " from=" fromStatus " to=" toStatus
                    " job=" job " printer=" printer " result=" result " ms=" ms:long " file=" file
| project timestamp, ep, item, fromStatus, toStatus, job, result, ms, file
| order by timestamp desc
```

**Query 2 — the weekly report you asked for**
```kusto
traces
| where timestamp > ago(90d) and message startswith "PRINT_EVENT"
| parse message with "PRINT_EVENT ep=" ep " item=" item " from=" fromStatus " to=" toStatus
                    " job=" job " printer=" printer " result=" result " ms=" ms:long " file=" file
| summarize
    submitted = countif(ep == "submit" and result == "submitted"),
    retried   = countif(ep == "poll"   and result == "requeued"),
    completed = countif(result == "completed"),
    failed    = countif(result == "failed" or result == "failed_terminal"),
    gaveUp    = countif(result == "gave_up"),
    cancelled = countif(result == "cancelled"),
    filesTouched = dcount(item)
  by week = startofweek(timestamp)
| order by week desc
```

**Query 3 — same, as a chart for the workbook**
```kusto
traces
| where timestamp > ago(90d) and message startswith "PRINT_EVENT"
| parse message with "PRINT_EVENT ep=" ep " item=" item " from=" fromStatus " to=" toStatus
                    " job=" job " printer=" printer " result=" result " ms=" ms:long " file=" file
| summarize count() by week = startofweek(timestamp), result
| render columnchart
```

**Query 4 — files retried more than once (chronic failures)**
```kusto
traces
| where timestamp > ago(30d) and message startswith "PRINT_EVENT"
| parse message with "PRINT_EVENT ep=" ep " item=" item " from=" fromStatus " to=" toStatus
                    " job=" job " printer=" printer " result=" result " ms=" ms:long " file=" file
| where ep == "poll" and result == "requeued"
| summarize retries = count(), lastTry = max(timestamp), any(file) by item
| where retries > 1
| order by retries desc
```
A file appearing here repeatedly is not a transient failure — it is a document the printer cannot
handle, and no amount of retrying will fix it.

**Query 5 — invocation health**
```kusto
traces
| where timestamp > ago(7d) and message startswith "RUN_SUMMARY"
| parse message with "RUN_SUMMARY ep=" ep " lib=" lib " printer=" printer " found=" found:int
                    " ok=" ok:int " failed=" failed:int " skipped=" skipped:int
                    " remaining=" remaining:int " httpStatus=" httpStatus:int " ms=" ms:long " folder=" folder
| summarize runs = count(), errors = countif(httpStatus >= 500),
            p95ms = percentile(ms, 95) by ep, bin(timestamp, 1d)
| order by timestamp desc
```

**Query 6 — did the schedule stop?** (the most dangerous failure is silence)
```kusto
traces
| where timestamp > ago(2h) and message startswith "RUN_SUMMARY"
| summarize lastRun = max(timestamp) by ep = extract("ep=(\\w+)", 1, message)
```

Save each with **Save → Save as query** so they are one click next time.

### 13.6 The Azure Portal answer — an Application Insights **Workbook**

This is the thing to leverage. Workbooks combine several KQL queries, charts and parameters into
one saved, shareable page, and pin to an Azure dashboard.

1. Function App → **Application Insights** → **Workbooks** → **+ New**
2. **Add → Add query**, Data source *Logs*, Resource type *Application Insights* → paste Query 3 →
   Visualization **Bar chart** → *Run*. Title it "Print activity by week".
3. **Add → Add query** with Query 2 → Visualization **Grid** — the numeric weekly table.
4. **Add → Add query** with Query 4 → Grid — "Chronic failures".
5. **Add → Add parameter** → a *Time range* parameter, so the whole page re-scopes at once.
6. **Save** as `Print pipeline — weekly`, then **Pin to dashboard** for one-click access.

Also worth pinning, from Function App → **Metrics**: `Requests` (Sum), `Http 5xx` (Sum),
`Response Time` (Avg). These come from the platform and exist even if App Insights is off.

### 13.7 Current state — SharePoint, with no code at all

For "how many are pending / failed / completed **right now**", the library answers directly and
exactly:

- **In SharePoint:** create a view grouped by `Print_Status`. Group headers show live counts.
  The second view this used to recommend — `PRINT_PENDING` **and** `Created < today-20`, to surface
  the G1/UC-10 stuck files — is **no longer needed**: past `giveUpDays` Poll writes `PRINT_FAILED`
  itself, so those rows now appear under the `PRINT_FAILED` group with a reason in `Print_Message`.
  A view worth keeping instead: `Print_JobId` is not empty **and** `Printer_Name` is empty, which is
  the one row shape Poll reports every run but cannot resolve (**G3**).
- **Flow D — Weekly digest** (Mondays 07:00): SharePoint **Get items** with
  `$filter=Print_Status eq 'PRINT_PENDING'` (repeat per status), take `length()` of each, and email
  the four counts plus a link to the workbook. Pure Power Automate — **no function code**.

> **Why no `/api/print/report` endpoint.** It would only re-expose what SharePoint already answers
> exactly, and what Power Automate can already read with a connector action. Adding an endpoint
> would create a second way to ask the same question, with its own bugs and tests. Skipped on
> purpose (§P-2 *Simplicity First*); the layering in §9 makes it trivial to add later if a caller
> genuinely needs one call.

### 13.8 Alerts — the failure worth catching is silence

| Name | Scope | Condition | Sev | Catches |
|---|---|---|---|---|
| `alert-print-5xx` | Function App | `Http Server Errors` ≥ 3 in 1 h | 2 | Graph/auth/printer breakage |
| `alert-print-silent` | App Insights | Query 6 returns no `submit` row in 2 h (log-search alert) | 2 | Flow off, connection expired, token revoked — **the failure mode with no other trace** |
| `alert-print-failed-batch` | App Insights | `PRINT_EVENT … result=failed` ≥ 5 in 1 h | 3 | A bad printer or a run of bad documents |
| `budget-print` | Resource group | 80 % actual / 100 % forecast | — | Cost guardrail |

Create one action group first (Monitor → Alerts → Action groups) with an email target, and point
all rules at it. Metric rules ~$0.10/month each; log-search rules ~$0.50/month.

### 13.9 Retention and cost

Application Insights tables keep **90 days free** — about 13 weeks of weekly history, which suits
a weekly report. Extendable to 730 days at cost via the workspace or per-table retention setting.
At this volume, telemetry stays inside the free ingestion grant; expect roughly **$1–2/month**
total, dominated by the alert rules.

If you ever need history beyond 90 days, the cheapest option is to keep the workbook's weekly
grid and paste it into a spreadsheet each quarter — far less than paying for extended retention on
a low-volume pipeline.
