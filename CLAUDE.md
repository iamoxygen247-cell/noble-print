# CLAUDE.md

Behavioural guidelines for this project. Platform: **Windows 11 · PowerShell · Python**.

**Tradeoff:** these bias toward caution over speed. For trivial tasks, use judgment.

---

## Project Context

Azure Function App that prints SharePoint documents through Microsoft Universal
Print, driven by Power Automate. The SharePoint library is the queue and the
audit trail; four columns per file carry the state.

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
| claim first | the print is lost — Resubmit recovers it after 72 h |
| claim last | the document prints **twice** — nothing recovers that |

A recoverable lost print beats a silent double print. `test_the_claim_precedes_
the_print_job` pins the ordering; do not "tidy" it.

Consequence: **a `PRINT_PENDING` row with an empty `Print_JobId` is normal**, not
an error. Poll must skip it (but still count it — `awaitingResubmit`); Resubmit
owns it.

Second consequence, and the one that already bit once (S1): **the PATCH that
writes `Print_JobId` must never be allowed to raise.** By then the job is created
*and* started — paper is on its way — so an unguarded failure leaves exactly the
row shape above, and Resubmit prints the document again 72 h later. The write
cannot be recovered; what it must do instead is keep going and say so, with a
`warning` on the item and an `ERROR` line naming the coming duplicate.

### 2. A stalled job is cancelled before it is replaced

Graph defines `stopped` as "an issue with the printer needs to be addressed
**before the job can continue**" — the job is alive. Resubmitting without
cancelling means the original and the replacement both print once someone clears
the jam. Cancel is best-effort: a failure still resubmits, but it is logged,
because that log line is the only warning a duplicate may appear.

Cancel is documented **only** on `/print/printers/{id}/jobs/{id}/cancel`, so it
needs the printer id, not the share id. The preflight captures it.

### 3. Two calls must NOT carry an Authorization header

* the SharePoint pre-authenticated download URL
* the Universal Print upload session `PUT` — Graph documents that adding a bearer
  token "might result in an HTTP 401"

Both go through `graph_client`'s separate unauthenticated session. Tests assert
the header is absent. A refactor toward "just use the authenticated client
everywhere" will break both, one silently.

### 4. `print_policy.py` imports only the standard library

That constraint is what keeps 353 tests offline and sub-second, and what makes the
utility reusable — another workflow keeps the adapters and replaces only the
rules. `test_print_policy_imports_only_the_standard_library` enforces it.

Corollary: **`function_app.py` contains no status branching.** An `if status ==`
in a route belongs in `print_policy`.

---

## Standard Commands

```powershell
.\.venv\Scripts\python.exe -m pytest              # 353 tests, offline, ~0.3s
.\scripts\start-local.ps1                         # venv + TLS shim + func start
.\.venv\Scripts\python.exe scripts\bootstrap_token.py
.\.venv\Scripts\python.exe scripts\test.py dryrun --library "Documents" --folder "/Invoices/ToPrint" --printer-share-id <guid>
```

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
| C | `test_submit/poll/resubmit/auth.py` | the routes, end to end |
| C | `test_requirements.py` | **conformance, one test per requirement clause** |
| C | `test_review_findings.py` | the six defects the first review found; each failed first |
| C | `test_second_review.py` | the five the second review found (S1–S5); each failed first |
| C | `test_acknowledged_time.py` | `printed on ...` uses the printer's `acknowledgedDateTime`, not our polling clock |
| C | `test_real_printer.py` | the real Brother registration — share-id vs printer-id |
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

Config that **names an environment** gets no default and raises when missing.
Config that is a **tunable** gets a default, an env override, and range
validation: an out-of-range *request* value is a 400; an out-of-range *env* value
warns and falls back.

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
