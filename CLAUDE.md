# CLAUDE.md

Behavioural guidelines for this project. Platform: **Windows 11 · PowerShell · Python**.

**Tradeoff:** these bias toward caution over speed. For trivial tasks, use judgment.

---

## Project Context

Azure Function App that prints SharePoint documents through Microsoft Universal
Print, driven by Power Automate. The SharePoint library is the queue and the
audit trail; five columns per file carry the state.

Use the project virtual environment, always by explicit path:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pip install -r functionapp\requirements.txt
.\.venv\Scripts\python.exe -m pip show PACKAGE
```

Never `pip install` bare, and never rely on global `python` (Microsoft Store
Python especially). `python -m pip` against an explicit interpreter path is the
only form that cannot install into the wrong environment.

## Related Project Memory Files

* `docs/design.md` — the full design: requirements trace, system design, use
  cases, reporting. **Read §2 (requirements trace) before changing behaviour** —
  several deviations from the literal requirement are deliberate and recorded
  there with their reasons.
* `docs/ai/project-playbook.md` — inherited engineering rules from the sibling
  invoice project. Read §2 before changing the module layout, §6–§7 before the
  first local run and the first deploy.
* `docs/live-test.md` — how to test against the real printer and SharePoint.
  Stage 1 needs no Function App and no app registration.
* `docs/deploy-to-azure.md` — production deployment, step by step, in the order
  that matters. Two steps fail *silently* if done out of sequence.
* `docs/ai/troubleshooting.md` — confirmed mistakes, failed commands, verified
  fixes. **Check it before starting any debugging or environment work.**
* `CLAUDE.local.md` — machine-specific notes. Gitignored.

---

## The four rules this design actually rests on

Break any of these and the pipeline prints things twice, or stops silently.

### 1. The claim precedes the print job

`_submit_one` writes `PRINT_PENDING` with an eTag-conditioned PATCH **before**
creating the job. The requirement writes the status afterwards; this is a
deliberate deviation (design R5) because the two orderings fail differently:

| ordering | a crash means |
|---|---|
| claim first | the print is lost — Poll requeues it and stamps the next boundary the file is owed; 15–30 min on a first attempt, longer for a file already deep in its backoff |
| claim last | the document prints **twice** — nothing recovers that |

A recoverable lost print beats a silent double print. `test_the_claim_precedes_
the_print_job` pins the ordering; do not "tidy" it.

Consequence: **a `PRINT_PENDING` row with an empty `Print_JobId` is normal**, not
an error. It is a crashed submission, and **Poll requeues it** — sets it back to
`PRINT_READY` so Submit picks it up again.

Second consequence, and the one that already bit once (S1): **the PATCH that
writes `Print_JobId` must never be allowed to raise.** By then the job is created
*and* started — paper is on its way — so an unguarded failure leaves exactly the
row shape above, and the document prints again. The write cannot be recovered;
what it must do instead is keep going and say so, with a `warning` on the item and
an `ERROR` line naming the coming duplicate. **Moving recovery into Poll made this
sharper**: the duplicate used to arrive 72 h later, and now arrives within the
file's next retry boundary — 15–30 minutes on a first attempt, before anyone reads
the log.

### 2. A stalled job is cancelled before it is replaced

Graph defines `stopped` as "an issue with the printer needs to be addressed
**before the job can continue**" — the job is alive. Requeuing without cancelling
means the original and the replacement both print once someone clears the jam.
Cancel is best-effort: a failure still requeues, but it is logged, because that
log line is the only warning a duplicate may appear.

The same applies when Poll **gives up**: the outstanding job is cancelled before
`PRINT_FAILED` is written, or an abandoned job could print days later against a
row claiming it failed.

Cancel is documented **only** on `/print/printers/{id}/jobs/{id}/cancel`, so it
needs the printer id, not the share id. `_cancel_outstanding` resolves it from the
share named in `Printer_Name` — **or from Poll's `printerShareId` when one was
sent**, which hard-overrides the row.

That override is defect **F3-R** and it is deliberate. Job ids are per-printer, so
an override naming a share the row's job does not live on cannot cancel it, and
the original prints beside its replacement. Exposure is **zero with one printer
registered** — the override equals `Printer_Name` on every row — and is otherwise
counted as `printerOverridden`, which counts configuration divergence: every
disagreeing row, job or not. The WARNING is graded to match — only a row with an
outstanding job can print twice, so only that one names F3. Do not "tidy" the
override into silence; the counter is the containment.

### 2a. Recovery is Poll's, on an exponential schedule

There is **no Resubmit endpoint** — it was retired 2026-09-01 and its work folded
into Poll, which runs every 10 minutes instead of daily.

Retry `n` falls **due** at file age `stallMinutes × (2ⁿ − 1)` — 5, 15, 35, 75 …
minutes by default. The count is **derived from the file's age, never stored**:
there is no attempt counter. `retries_due` evaluated at the current job's creation
time says how many retries preceded it, because every requeue makes a new job. Do
not add a counter column to "simplify" this.

**The waiting happens in Submit, not Poll.** A stalled job is requeued *immediately*
and the row carries `Print_Time` — when retry `spent + 1` falls due. Submit refuses
to claim a file until that moment passes. Poll used to hold the row at
`PRINT_PENDING` and re-derive the same "not yet" on every run, which made the
backoff invisible; the schedule is identical, but now it is written down. A due
time in the **past** is normal and means "print now".

Consequence worth knowing before reading a live run: **the cadence that costs you
retries is Flow A's, not Flow B's.** Flow B at 10 min and at 1 min produce identical
retry sequences, because `Print_Time` pins the instant. But `spent` is read off the
file's age when the *first* job is created, so Flow A every 15 min opens the
schedule at retry 3, every 5 min at retry 2, every minute at retry 1. Lowering
`stallMinutes` compresses the ladder and makes this worse, not better.

**One bound: `giveUpDays`.** `maxRetries` is gone — it bounded the *work* while
`giveUpDays` bounded the *waiting*, which was two answers to one question, since the
count is derived from age anyway. `next_retry_time` **clamps** to
`created + giveUpDays`, and that clamp is load-bearing: a waiting row sits at
`PRINT_READY`, where Poll (which queries `PRINT_PENDING` only) cannot see it, so
anything scheduled past the deadline would print days late against a row that then
reads `PRINT_FAILED`. There is no grace period any more.

Both knobs, plus `batchSize`, are read from the **Power Automate request body** and
nowhere else — no app-setting fallback, so there is exactly one place to look when
the pacing is not what someone expected.

`retries_due` is uncapped and that is safe: the ladder doubles, so the loop is
logarithmic, and `poll_decision` only reaches it after `within_window` has bounded
the file's age. Across every legal pair of knobs it returns at most **19** — so the
highest retry number ever scheduled is 20.

### 2b. The upload format is chosen, not only inferred

`printing/` is a registry, and there are **two ways to pick a profile**:

| | asks | used when |
|---|---|---|
| `matches(share, source)` | can this profile serve this printer? | no `printFormat` — the original behaviour |
| `produces(format, source)` | can this profile emit this format? | Submit was sent a `printFormat` |

They disagree on purpose: `PwgRasterProfile.matches` stands aside when the printer
also accepts PDF, because rasterizing what the device takes natively is wasted
work. `printFormat` is how a caller overrules that. **Adding a format is a profile
class, one entry in `PROFILES`, and its MIME type in `SUPPORTED_PRINT_FORMATS`** —
no route change, which is the whole point of the registry.

Two refusals, deliberately different: a format **the printer** does not report is a
**400 at preflight** with nothing claimed; a format **this document** cannot
produce is a per-file `PRINT_FAILED`. Do not collapse them — the first is a broken
flow, the second is one bad file among good ones.

### 2c. Health runs first and writes nothing

`POST /api/print/health` is a pre-flight: one share read, **no writes anywhere**,
safe to call on every flow tick. It exists because every failure here was
otherwise found mid-run and several only after files were claimed.

It answers **200 with `healthy: false`** for a sick printer — never a non-2xx.
400 means the request was malformed, 500 means Health itself broke. Power Automate
marks a non-2xx as a failed action and halts the branch, which is precisely when
the diagnosis in the body is needed. `healthy`, `errors` and `warnings` are on
every 200 so a flow condition needs no null check (the S3 lesson).

Three conditions are what it is *for*. `PRINTER_STOPPED` is checked nowhere else
at all. The other two are noticed only too late to help: `NO_PRINTER_ID` by
`cancel_job`, as a WARNING at the moment a cancel is attempted — by which point
the duplicate is unavoidable (rule 2); and `CONVERTER_UNAVAILABLE` one claimed
file at a time, because `pypdfium2` is imported lazily inside `convert_pdf`.

The codes in `print_policy.ALL_HEALTH_CODES` are a **closed, stable vocabulary** —
a flow condition and a KQL query key on them, so renaming one is a breaking change
that no Python would catch.

### 3. Two calls must NOT carry an Authorization header

* the SharePoint pre-authenticated download URL
* the Universal Print upload session `PUT` — Graph documents that adding a bearer
  token "might result in an HTTP 401"

Both go through `graph_client`'s separate unauthenticated session. Tests assert
the header is absent. A refactor toward "just use the authenticated client
everywhere" will break both, one silently.

### 4. `print_policy.py` imports only the standard library

That constraint is what keeps 570 tests offline and sub-second, and what makes the
utility reusable — another workflow keeps the adapters and replaces only the
rules. `test_print_policy_imports_only_the_standard_library` enforces it.

Corollary: **`function_app.py` contains no status branching.** An `if status ==`
in a route belongs in `print_policy`.

---

## Standard Commands

```powershell
.\.venv\Scripts\python.exe -m pytest              # 570 tests, offline, ~1.0s
.\scripts\start-local.ps1                         # venv + TLS shim + func start
.\.venv\Scripts\python.exe scripts\bootstrap_token.py
.\.venv\Scripts\python.exe scripts\test.py dryrun --hostname noblehomes.sharepoint.com --site-path /sites/PM --library "AI_DropBox_V2026" --folder "/Backup/Invoice" --printer-share-id <guid>
```

`--hostname` and `--site-path` are **required** for `submit`, `dryrun` and
`status`: the site is request input now, not an app setting. `health` needs
neither.

If a command fails, find the failing layer before editing: command syntax →
working directory → interpreter → venv → dependency → env var → path → external
service → application code → test expectation. Do not fix a symptom before
confirming the cause.

## Testing

Five tiers, all but the last offline (design §10):

| Tier | File(s) | Covers |
|---|---|---|
| A | `test_print_policy.py` | the rules — windows, state mapping, formatting |
| B | `test_adapters.py` | the adapters against `FakeGraph` |
| C | `test_submit/poll/auth.py` | the routes, end to end. **Poll carries the retry schedule, the cancel-first guard, the give-up path, the `Print_Time` clamp and the `printerShareId` override (F3-R). Submit carries the due filter** |
| A+C | `test_printing.py` | the PWG encoder (byte-pinned), profile selection, and Submit against a raster-only printer |
| A+C | `test_health.py` | the pre-flight endpoint — the finding matrix as pure rules, the route, and **parity with Submit's dryRun** so Health cannot describe a pipeline Submit would not run |
| A+C | `test_print_format.py` | `printFormat` — the `matches`/`produces` split, the two refusals, and that the requested format reaches the wire |
| C | `test_requirements.py` | **conformance, one test per requirement clause** |
| C | `test_review_findings.py` | the six defects the first review found; each failed first |
| C | `test_second_review.py` | the five the second review found (S1–S5); each failed first |
| C | `test_acknowledged_time.py` | `printed on ...` uses the printer's `acknowledgedDateTime`, not our polling clock; and the stall clock reads `createdDateTime`, not it |
| C | `test_dryrun.py` | the dry run, and that the 20-day strand (G1) is fixed rather than merely reported |
| A | `test_make_test_pdf.py` | the live-test document is a structurally valid PDF |
| D | `scripts/test.py` | the live host — **not** collected by pytest |
| E | the deploy smoke sequence | design §11 |

`FakeGraph` is a fake `requests.Session`, not a mock of our own functions, so the
**real** adapter code runs against it. Its column names default to the *encoded*
form (`Print_x005f_Status`), so every field test is also a test that no display
name was hardcoded into a Graph call.

**The §5.4 write matrix in the design is the specification for Tier C** — one
test per row. If you change what a route writes, change the matrix first.

**Never weaken a test to make a change pass.** Red means investigate.

## Universal Print constraints — verified, not assumed

Written down because they are counter-intuitive and re-deriving them costs hours:

- Creating, starting and cancelling a print job are **delegated-only**;
  `Application: Not supported`. This is why there is a service account and a
  refresh token in Key Vault, not a managed identity.
- Job ids are **per-printer**, not globally unique. `Print_JobId` is only
  meaningful together with `Printer_Name`.
- `$orderby` on `fields/*` is not documented for SharePoint list items and is
  widely reported to fail; only one indexed field may be filtered at a time. So
  the query filters on status alone and Python does the ordering and windowing.
- **`Print_Status` must be indexed in SharePoint.** A non-indexed column cannot be
  used in `$filter` at all.
- SharePoint fixes internal column names at creation and may encode them.
  **Never hardcode an internal name**; `sharepoint.resolve_list` resolves them.

## Secrets and Configuration

Never hardcode secrets, keys, tokens, connection strings or function keys in
source, tests, docs or sample payloads. Use `YOUR_API_KEY`-style placeholders.

`functionapp/local.settings.json.template` is **tracked** with every key and blank
secrets; the real `local.settings.json` is gitignored and never deployed.

`PRINT_REFRESH_TOKEN` is a local-development escape hatch. It logs a warning on
every use so that if it ever reaches Azure the evidence is in App Insights.

Config comes in three kinds now:

* **Names the Azure environment** — `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`,
  `KEY_VAULT_URI`. App settings, no default, raise when missing.
* **Names the SharePoint environment** — `sharepointHostname`,
  `sharepointSitePath`. **Request input**, required, **400** when missing. These
  were app settings; moving them means one Function App serves any site a flow
  names, and it also means **whoever holds the function key chooses the site**,
  bounded only by what the delegated service account can reach. That widening is
  deliberate and is the reason this paragraph exists — do not "restore" the app
  settings without deciding that trade again.
* **Flow-facing tunables** — `batchSize`, `giveUpDays`, `stallMinutes`. Request
  body → default, with range validation and **no env override**. An out-of-range
  request value is a 400. A leftover `PRINT_BATCH_SIZE`, `PRINT_GIVE_UP_DAYS`,
  `PRINT_STALL_MINUTES` or `PRINT_MAX_RETRIES` app setting does nothing at all.

**Host tunables** keep the old shape — default, env override, range validation,
with a bad value warning and falling back: `PRINT_BUSINESS_TZ`,
`PRINT_BUDGET_SECONDS`, `GRAPH_TIMEOUT_SECONDS`, `PRINT_RASTER_*`. No flow sets
these. `PRINT_BUSINESS_TZ` matters more than it used to: `Print_Time` is written in
that zone.

## Observability

**Sampling must stay disabled in `host.json`.** Adaptive sampling drops `traces`
rows with no error anywhere, which would make every count in the weekly workbook
quietly wrong.

Two log lines carry all reporting, with free text **last** so KQL `parse` stays
unambiguous:

```
RUN_SUMMARY  one per invocation
PRINT_EVENT  one per file per outcome
```

Tests assert both formats. Changing a field name or its position breaks the
workbook silently — see design §13.

## Generated Documents

Do not publish plans, reviews or reports to externally hosted pages. Write
HTML/Markdown deliverables into `docs/` and reference them by path.

## Completion Checklist

* The request was satisfied; the solution is no more complex than necessary.
* Only necessary files changed; no unrelated refactoring.
* No secrets added.
* Python commands used the project venv by explicit path.
* `pytest` was run and is green — and if it is not, the output is shown, not hidden.
* Any reusable mistake was proposed for `docs/ai/troubleshooting.md`.
