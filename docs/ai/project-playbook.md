# Project Playbook — reusable architecture, practice, and setup learnings

Portable engineering knowledge distilled from the Noble invoice-processing project
(Windows 11 · Python · Azure Functions · Azure Content Understanding · Power Automate).

Every rule here was paid for once. The **Rule** is the transferable part; the *Evidence*
line records what it cost to learn, so a future reader can judge whether it applies.

**Scope:** §1 and §8–§11 are general-purpose. §2 is architecture for any service-backed
pipeline. §3–§4 apply to any pipeline built on a **non-deterministic service** — an LLM, an
OCR/extraction API, a classifier, a recommender — where the same input can produce a different
output tomorrow. §5–§7 are Windows · Python · Azure Functions specifics.

---

## How to use this in a new project

1. Copy this file into the new repo as `docs/ai/project-playbook.md`.
2. Copy the skeletons in §11 into `CLAUDE.md`, `docs/ai/troubleshooting.md`, and
   `docs/ai/open-defects.md`.
3. Add a line to the new `CLAUDE.md`:

   ```markdown
   ## Related Project Memory Files
   * `docs/ai/project-playbook.md` — inherited engineering rules. Read §2 before designing
     the module layout, §3–§4 before wiring anything to a non-deterministic service, and
     §6–§7 before the first local run and the first deploy.
   ```
4. Work through the day-one checklist in §10.

Prune as you go. A rule you cannot connect to your project is noise, and the value of this
file is that everything in it is load-bearing.

---

## 1. First principles

These sit above everything else and are the ones most often violated under time pressure.

| # | Rule |
|---|---|
| 1 | **Don't assume — surface the ambiguity.** If two readings of a requirement lead to different work, ask. If progress is safe under an explicit assumption, state it and continue. |
| 2 | **Simplicity first.** No speculative abstraction, no configurability nobody asked for, no error handling for impossible states. If 200 lines could be 50, rewrite it. |
| 3 | **Surgical changes.** Every changed line traces to the request. Don't "improve" adjacent code. Clean up orphans *your* change created; leave pre-existing dead code alone and mention it. |
| 4 | **Goal-driven execution.** Convert the task into a verifiable check before writing code: "add validation" → "write tests for invalid inputs, then make them pass." Weak criteria ("make it work") need clarification, not optimism. |
| 5 | **Measure, don't impress.** "5/10 runs" is actionable; "sometimes" is not. This applies to bug reports, regressions, and performance alike. |
| 6 | **A mistake is data.** Record what was wrong, why it happened, and the corrected rule — in a durable file, not the conversation. If the same mistake happens twice, promote it from a troubleshooting note to a project rule. |

---

## 2. Architecture patterns worth reusing

### 2.1 One repo, one deployable folder

```
repo/
├─ <deployable>/     # the ONLY thing that ships (functionapp/ here)
│  ├─ <entrypoint>.py    # host expects it at this folder's root — don't move it into src/
│  ├─ <domain rules>.py  # pure, no cloud SDK, offline-testable
│  ├─ <orchestrator>     # thin: validate → claim → call → decide → persist → respond
│  └─ requirements.txt, local.settings.json.template (gitignore the real one)
├─ scripts/          # harnesses, provisioning, regression — NOT deployed
├─ tests/            # offline suite, no cloud dependency
├─ analyzers/ (or prompts/, config/)  # service definitions, version-controlled
├─ docs/  docs/ai/   # design docs + durable AI/engineering memory
└─ samples/  out/    # real inputs + generated output — both gitignored
```

**Rule.** The deployment unit is a folder, not the repo. Packaging from it ships a clean
artifact and can never accidentally include harnesses, customer PDFs, or a venv.

**Rule.** Shared logic lives in **exactly one place** — inside the deployable — and the
scripts and tests import it via a small `sys.path` bootstrap. Never let a harness carry its
own copy of a threshold or a rule.

*Evidence:* the harness scores with the identical gate code the production function runs, so
a threshold change is one edit, and a dedicated test (`test_harness_scorecard.py`) asserts the
harness and the service agree.

```python
# top of any script or test that needs the deployable's modules
_REPO = pathlib.Path(__file__).resolve().parent.parent
_APP = _REPO / "functionapp"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))
import gates, field_policy   # noqa: E402 — after the bootstrap
```

### 2.2 Separate *parsing*, *rules*, and *orchestration*

Three layers, three files, three reasons to change:

| Layer | Knows about | Must not know about |
|---|---|---|
| `cu_client.py` (adapter) | the external service's SDK, auth, transport, timeout | business rules |
| `gates.py` (parsing + decisions) | the service's *output shape* | which fields matter, what a threshold is |
| `field_policy.py` (rules) | thresholds, criticality, derivations, defaults | the service's JSON, any cloud SDK |
| `function_app.py` (orchestrator) | the sequence | any domain branching whatsoever |

**Rule.** The rules module operates on a *parsed* representation (`{field: (value, confidence)}`),
imports nothing from the cloud SDK, and is standard-library only. That single constraint is what
makes 112 unit tests run in 0.4 s with no cloud account, no emulator, and no network.

**Rule.** The orchestrator contains **no** domain branching. If you find an `if bill_type ==`
in the entry point, it belongs in the rules module.

**Rule.** Name a *single source of truth* per concept and comment it as such — one threshold
constant, one critical-field list, one date-format constant. Add a `POLICY_VERSION` string and
stamp it on every persisted record, so you can tell later which rule set produced a row.

### 2.3 Base + delta beats per-type rule sets

Requirements differed by document type, so the rules could have been two parallel lists. They
aren't:

```python
BASE_CRITICAL    = ("vendor_name", "service_address", "total_invoice_amount")
COMMERCIAL_DELTA = ("po_or_job_number", "gst_amount")
MUNICIPAL_DELTA  = ("account_number", "invoice_number")
```

**Rule.** Express type-dependent policy as **base + delta**. A misclassification can then only
ever drop a *delta* requirement, never a base one — the failure mode is bounded by construction
rather than by care. Keep the bucket-dependent surface to one function (`critical_fields(bucket)`);
a future third type then touches one file.

**Rule.** Decide the type from the *classification label only*, never inferred from the presence
or absence of other fields. Otherwise changing a field policy silently changes the type
determination, and the two failures become impossible to separate.

### 2.4 Idempotency, claims, and who owns each write

**Rule.** Claim before you work. An atomic claim keyed on the *source system's* stable id
(here a SharePoint item GUID, via an etag-guarded Table Storage row) stops two concurrent
invocations doing the same expensive work.

**Rule.** Validate the request **before** the claim. A malformed request that 400s after
claiming would block its own corrected retry.

**Rule.** Release the claim on failure. A crash that leaves a claim held converts a transient
error into a stuck item until the lease expires.

**Rule.** Be explicit about *non*-requirements too. Here: "gate A1 is a concurrency guard only —
it is NOT duplicate detection; the same item posted twice is fully re-processed, by requirement."
Writing the non-requirement down prevents a future contributor from "fixing" it.

**Rule.** Exactly one component owns each write. The function writes `Received` then
`Extracted`; the *flow* writes the terminal `Written` + record id after the destination returns
201. Two writers to one field is a race you will debug at 2 a.m.

### 2.5 State vs evidence: two stores, two lifetimes

| Store | Holds | Why |
|---|---|---|
| Table row (ledger) | processing state, decision, policy stamps, timings, blob pointers | small, queryable, cheap |
| Blob sidecar per run | the **full raw service response** + the decision JSON | 64 KB/property and 1 MB/entity limits make this impossible in a table |

**Rule.** Persist the raw response of every non-deterministic call, timestamped, keyed by the
row. When a bad output is reported weeks later, this is the only thing that can tell you whether
the service, your parsing, or your rules were at fault. It is also your free A/B corpus (§4.4).

**Rule.** Diagnostics are **best-effort by contract**: catch every exception, log a warning,
return `None`. A diagnostics outage must never fail the real work. Say so in the module
docstring so nobody "improves" it into a hard dependency.

**Rule.** Set retention as a platform lifecycle rule on the container, not as code. Application
code that deletes its own evidence is a bug waiting to happen.

### 2.6 Timeouts must undercut the caller's budget

```python
# Must undercut the caller's hard ~120 s connector budget (which also covers
# upload and cold start), so the structured 502 + claim release reaches the
# caller before it times out and its retry sees a released row.
DEFAULT_ANALYZE_TIMEOUT_SECONDS = 100.0
```

**Rule.** Cap every long-running call, and pick the cap from the **caller's** timeout minus
overhead — not from a round number. An uncapped call holds the claim until the platform kills
the process, and the caller's automatic retry then races your failure handler.

### 2.7 Configuration that cannot silently target the wrong environment

**Rule.** Config values that *name an environment* (service endpoint, account name) get **no
baked-in default**. Missing → raise. A silent fallback lets a misconfigured app write to
another environment's data.

**Rule.** Config values that are *tunables* (a threshold) get a constant default, an env
override, and **range validation**. An out-of-range request value is a 400; an out-of-range
env value logs a warning and falls back, because server misconfiguration should not fail
every request.

**Rule.** **A code default is not the live value.** Query the deployed app's settings before
quoting a number in a doc, an incident report, or a decision.

*Evidence:* a staleness lease read from the source constant gave 600 s; the live value was
300 s, set as an app setting. The wrong number invalidated the retry-interval inequality that
prevents a silent-drop failure mode.

### 2.8 One stable log line per invocation

```python
logging.info("RUN_SUMMARY sourceId=%s httpStatus=%s status=%s decision=%s cuMs=%s", ...)
```

**Rule.** Emit exactly one greppable, parseable summary line per invocation, with a stable
prefix, and build every alert and dashboard query on that line. Embed the fields in the
message text when the runtime does not reliably map structured extras (the Python worker
does not map `extra=` into `customDimensions`).

**Rule.** Use a sentinel (`-`, `-1`) for "never reached", never an omitted key — a missing
key breaks the query, a sentinel is a countable fact.

---

## 3. Building on a non-deterministic service (LLM / AI extraction)

This is the part that generalizes least obviously and matters most. Everything below was
learned against Azure Content Understanding, but applies to any prompt-driven extraction.

### 3.1 The output is a distribution, not a value

**Rule.** Treat every field as a *rate*, not a result. "It works" is meaningless; "correct
11/12" is a fact. Confidence scores are themselves noisy — classification confidence moved
±0.3 run-to-run on identical documents here, while the label stayed stable.

**Rule.** Never tune a threshold from one run.

### 3.2 Twin fields: extract + generate

Each important field is captured **twice** by the analyzer — an `extract` twin (span-grounded,
so its confidence is meaningful) and a `generate` twin (normalized/reasoned) — and the code
resolves them:

| Resolution style | Used for | Behaviour |
|---|---|---|
| **Combine** | `vendor_name` | either twin clearing the bar can satisfy the requirement; code computes the final |
| **Validator** | `service_address` | extract is authoritative; the generate value is *never written*, it can only rescue a below-threshold extract when the two agree |
| **Backfill** | `bill_to_address` | not critical, not written; exists solely to fill another field when the document lacks that block |

**Rule.** Decide per field whether the second opinion is a *peer*, a *validator*, or a
*fallback source*, and write it in the constants block. "We have two values" is not a design.

**Rule — the agreement boost is not a safeguard against correlated failure.** Two twins that
are both wrong *in the same way* agree, and an agreement rule then promotes the pair above the
threshold.

*Evidence:* a municipal letterhead offered two parses — `City of Delta` (conf 0.883) and bare
`Delta` (conf 0.416). On a slip, **both** twins returned `Delta` (0.416 / 0.343) — far below the
0.73 bar — and "agreement" promoted them to a passing resolution. Twin disagreement, the main
safeguard, cannot protect against a failure that moves both twins the same way. Guard the
*specific known shape* in code (here: a printed prefix restores the full name).

### 3.3 Prefer code-side fixes to prompt edits

**Rule.** When a field is wrong, first ask whether the correct answer is *derivable from what
you already receive*. Fix it in code. Edit the prompt only when the information genuinely is not
in the response.

*Evidence, all of it pointing the same way:*
- Editing one field's description **destabilizes other fields** (~20 % swing in an unrelated
  date field). There is cross-field coupling inside the model you cannot see or bound.
- Editing only *character limits* in five narrative descriptions damaged `vendor_name` and
  `account_number`.
- Four successive prompt attempts to fix a trade-name ("dba") issue all failed or measured
  *harmful*; a code-side fix shipped in one commit with the analyzer untouched — and therefore
  needed no production push at all.
- When the upstream service itself regressed (§4.5), the code-side guard shipped hours earlier
  absorbed it completely: replaying 96 affected reads gave 96/96 repaired. No prompt work would
  have caught that.

**Corollary.** A code-side fix is cheaper to ship, cheaper to test offline, cheaper to revert,
and has a blast radius you can actually enumerate.

### 3.4 Prompt-writing rules that measurably worked

- **Give weak fields a numbered reasoning procedure.** Turning a `generate` field's description
  into explicit steps ("1. Look for… 2. If absent… 3. Return…") measurably improved the hardest
  fields. A contract test asserts the numbered steps stay sequential.
- **Character limits must be expressed as characters plus drop-word guidance.** A word-count
  budget does not cap characters.
- **Positive instruction beats prohibition** where possible; state what to return *and* an
  explicit list of what not to return (customer, bill-to party, attention line, contact person).
- **Ask for coverage, not for a judgement.** A prompt told to suppress a disclaimer worked 10/10
  in isolation but destabilized the corpus; "report what is covered" was the stable framing.
- **Never let a prompt invent a value.** A `generate` twin will answer even when the field is
  not printed — one returned a footer print-timestamp as an invoice date at 0.82 confidence,
  another invented a `1` day count. **Guard that in code, not in the prompt.**

### 3.5 Never silently default

**Rule.** A defaulted value that looks plausible is worse than a missing one, because no human
ever reviews it.

*Evidence:* a date field silently became "today" on runs where its confidence dipped; a
"+30 days" due-date default wrote dates ~8 days late, unseen, for weeks. Both were only found
because a sidecar assertion kept getting removed to make the suite green — **the repeated
removal was the symptom.**

**Rule.** If you must default, record which fields were defaulted on the row (`DefaultedFields`),
and treat a rising defaulted-rate as an alert.

---

## 4. Testing a system whose core is non-deterministic

A unit suite cannot test a prompt. Without something else, prompt edits ship unverified. The
two-tier net below is the reusable answer.

### 4.1 Tier A — offline contract tests over the service definitions

The prompt/analyzer JSONs drive every field, yet nothing read them under test: the suite passed
green while a renamed field produced nulls in production.

**Rule.** Assert the *contract between the definition file and the code*, deriving the
expectations **from the code** rather than from a hardcoded list, so they stay true as the schema
evolves:

- every field has a valid method and a non-empty description
- the definition's field set matches the code's expected field list exactly
- every twin constant in the code exists in the JSON with the right method
- twin pairs share a type; value types match what the parser actually parses
- classification enums match the policy module's buckets
- numbered reasoning steps are sequential
- the router's child-analyzer id is the one the code reads

Pure JSON + introspection: no network, no cloud account, runs in about a second.

### 4.2 Tier B — a golden corpus with per-document expectation sidecars

```
tests/pre-commit-test/
├─ <doc>.pdf                # real customer document — GITIGNORED
└─ <doc>.expected.json      # the assertions — TRACKED, so they're reviewable in the diff
```

The runner provisions the **working-tree** definition as a throwaway analyzer, pushes each
document through **the same decision path production uses**, and compares against the sidecar.

**Rule.** Ignore the *contents* of the corpus directory but negate the sidecars, so the
assertions are shared and reviewable while the customer data never enters git history:

```gitignore
tests/pre-commit-test/*
!tests/pre-commit-test/*.expected.json
```

**Rule.** A missing or empty corpus folder is a **clean skip**, so a fresh clone still passes.

**Rule — three outcomes, not two.** `OK` (matches on every replicate), `WRONG` (disagrees),
`UNSTABLE` (replicates disagree *with each other*). Exit non-zero on `WRONG` **or** `UNSTABLE`.
Without the third outcome you cannot distinguish "broken" from "coin-flip", and coin-flips are
the majority of what you will find.

**Rule.** Assert the **full stable output set** per document, not a convenient subset. A field
asserted on one document has a sample size of one, and the next edit breaks it silently
elsewhere.

**Rule.** Scaffold sidecars, then **delete every value you have not personally verified against
the source document.** A sidecar is a hard assertion; an unverified value is a false failure
waiting to fire.

### 4.3 Make it free to run: content-addressed caching

**Rule.** Cache each result keyed by `(definition hash, input bytes hash, replicate index)`.
When neither the prompt nor the inputs changed, a full corpus run makes **zero API calls** and
just re-scores cached raw JSON offline — about a second. That is what makes it affordable on
*every commit*, and it means a pure code change to the parsing or rules layers gets full corpus
coverage at zero API cost.

**Rule.** Wire it as a two-tier pre-commit hook with a **narrow** escape hatch:

```
Tier A  pytest              — always, ~1 s, offline, blocks on failure
Tier B  corpus regression   — cache hit ⇒ ~1 s / 0 calls; definition changed ⇒ real calls
Escape  SKIP_REGRESS=1 skips ONLY tier B, prints a loud notice. Never for a prompt change.
```

**Rule.** `.git/hooks` is not versioned. Track the hook at `scripts/hooks/pre-commit` and ship
an installer — **and remember the tracked file is not the file that runs.** Re-run the installer
after editing it.

**Rule.** Gate the production push on the net. Pushing the prod definition requires a green
regression stamp (`out/regress/<HEAD-sha>.json`) for a **clean** working tree; a dirty tree is
refused because the stamp would not describe what is being pushed. Gate on the *literal* prod
id, not an env-overridable one, so the gate cannot be dodged by setting a variable.

**Rule.** Never weaken the net to make a change pass — don't delete a corpus document, loosen a
sidecar, or force-push the definition. Red means investigate.

### 4.4 Statistics you actually need

| Rule | Evidence |
|---|---|
| **n ≥ 12 per arm** for any comparison that drives a decision. | n=5 cannot separate 4/5 from 3/5; a 5/5 sample of a 67 %-true field is unremarkable. A regression was claimed on n=5 evidence twice and withdrawn both times. |
| **A green cached run proves nothing about stability** — only that the cache still holds the roll it was written from. | A forced re-roll exposed three long-standing coin-flip assertions that had been frozen from one lucky roll: one "passing" assertion was really a 2/12 outcome. |
| **A red run is not automatically your diff's fault.** | Of six rows that went red on a change, **none** was caused by it; all six were pre-existing instabilities, exposed because the edit invalidated the cache. |
| **Don't A/B 400 calls when 20 will do.** | Isolate the one document and field under test and run replicates on *it*, rather than re-rolling the whole corpus. |

### 4.5 The confound that will get you: version is entangled with wall-clock time

**Rule. Attributing a regression to a definition edit requires a *concurrent* control.**
Re-roll the **old** definition *the same day*, beside the new one, n ≥ 12 per arm. Never compare
a new version against cached numbers from an older one.

*Evidence — the most expensive lesson in this project.* A field returned the correct value on
**66 of 66** cached reads across 14 definitions over four weeks, then slipped on exactly the two
versions that touched one unrelated prompt. p ≈ 3×10⁻⁴. Two edits were reverted on that evidence.
Both reverts were wrong.

A controlled experiment — three definitions plus live prod, n = 24 each, same afternoon, one
scratch analyzer — found **no difference between any arm** (10/24, 12/24, 14/24, 8/24;
p = 0.39, 0.19). The **service itself** had stepped from ~0 % to ~40 % under a fixed definition.
The edits were bystanders.

The trap is subtle: **the historical baseline was real.** 0-in-66 at a true 40 % rate is ~10⁻¹⁴,
so the old regime genuinely existed. What was invalid was using a measurement from weeks earlier
as a *control* for one taken today.

**Corollaries.**
1. Cached history records what the service did *then*. It is a regression net for **your code**,
   not a fixed yardstick for **their service**.
2. Roll every arm on a **scratch** copy, never prod, and roll them in the same session.
3. **Verify the push actually swapped the definition** — fetch it back and compare a description
   length against the local file. Otherwise a null result is indistinguishable from a silent no-op.
4. A corpus failure rate describes the *test* analyzer, not production. Don't quote it as a
   production number.

---

## 5. Environment and tooling (Windows · Python · Azure)

### 5.1 Always name the interpreter explicitly

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m pip show PACKAGE
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable)"
```

**Rule.** Never `pip install` bare and never rely on global `python` (Microsoft Store Python
especially). `python -m pip` against an explicit interpreter path is the only form that cannot
install into the wrong environment.

**Rule.** A Python **minor** upgrade that relocates the base install layout kills an existing
venv (a patch bump does not). Recreate the venv rather than debugging the imports.

### 5.2 Corporate TLS inspection

A TLS-inspecting agent makes OpenSSL-based clients fail `CERTIFICATE_VERIFY_FAILED` while
.NET-based tooling works, because .NET trusts the Windows certificate store.

**Rule.** Inject `truststore` at the top of every script that makes an outbound call, and clear
any stale CA-bundle override:

```python
os.environ.pop("REQUESTS_CA_BUNDLE", None)
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:      # no-op where truststore isn't installed (e.g. in the cloud)
    pass
```

**Rule.** A vendored CLI with its **own bundled Python** needs the same treatment via a wrapper
or a `sitecustomize.py`. And a shell-function wrapper only helps commands *you* type — a tool
that spawns a child process resolves the binary from `PATH` and needs a real shim file there.

**Rule.** Machine-specific paths, shim locations, and revert instructions belong in a
**gitignored** `CLAUDE.local.md`, never in a committed file.

### 5.3 Encoding traps on Windows

| Trap | Fix |
|---|---|
| A *redirected* stdout defaults to cp1252, so printing non-ASCII raises `UnicodeEncodeError` — and a pre-commit hook runs exactly that way, burying a real failure under an encoding traceback. | `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at the top of any script that prints data values. |
| An editor writes a settings JSON with a BOM; `json.loads` fails with `Unexpected UTF-8 BOM`. | Read with `encoding="utf-8-sig"`. |
| Writing Python (or any backslash-heavy text) through a shell heredoc mangles backslashes. | Write a real file with a file tool, or build the string with `chr()`. |
| `zoneinfo` raises `ZoneInfoNotFoundError` on a host with no IANA database. | Add `tzdata` to `requirements.txt`; harmless on Linux, required on Windows. |

---

## 6. Local development: make `func start` the primary loop

**Rule. Run the real host locally from day one.** `func start` boots the same Functions runtime,
the same entry point, the same trigger bindings, and the same Python worker the cloud app runs.
Only the services you deliberately leave remote are remote. A project that can only be exercised
after a deploy has a feedback loop measured in minutes and a debugging surface that includes the
entire platform.

Three loops, in the order to reach for them:

| Loop | Command | Cost | Covers |
|---|---|---|---|
| Offline unit suite | `.\.venv\Scripts\python.exe -m pytest` | 0.4 s, no network | rules, parsing, decisions, definition contracts |
| **Local host** | `func start` + the harness | seconds, one real service call | HTTP contract, bindings, config wiring, auth, storage writes, the whole decision path |
| Corpus regression | `regress.py` | ~1 s on a cache hit | prompt/definition behaviour over real documents |

Reach for the local host when the question involves the *wiring*; reach for pytest when it
involves the *rules*. Most "the function is broken" questions are pytest questions.

### 6.1 The settings template is a committed artifact

```
functionapp/
├─ local.settings.json.template   # TRACKED — every key, secrets EMPTY
└─ local.settings.json            # GITIGNORED — the real values
```

```json
{
  "IsEncrypted": false,
  "Values": {
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",

    "AZURE_SERVICE_ENDPOINT": "https://YOUR-RESOURCE.services.ai.azure.com/",
    "AZURE_SERVICE_KEY": "",
    "AZURE_STORAGE_ACCOUNT": "YOUR_STORAGE_ACCOUNT",
    "AZURE_TABLES_CONNECTION_STRING": "",
    "AZURE_CLIENT_ID": "",
    "FIELD_CONFIDENCE_THRESHOLD": "0.73"
  }
}
```

**Rule.** The template lists **every** key the app reads, with secret values blank. It is the
only documentation of the config surface that cannot drift, because a missing key breaks the
next person's first run.

**Rule.** `local.settings.json` is **never deployed** — the cloud app reads Application
Settings. Say that in the README, because "it works locally" otherwise becomes a config
mystery after every deploy (§7.3).

**Rule.** Scripts that read this file must use `encoding="utf-8-sig"`. Windows tooling writes it
with a BOM; PowerShell's `ConvertFrom-Json` strips it silently, Python's `json.loads` does not.

### 6.2 Gotcha #1 — Core Tools runs *its own* Python unless the venv is **activated**

*Symptom:* the host starts, the worker dies with `ModuleNotFoundError` for a package you
definitely installed (here `tzdata`, cascading into `ZoneInfoNotFoundError`), and the port never
comes up.

*Cause:* Core Tools spawned its bundled interpreter from
`...\Azure Functions Core Tools\workers\python\...` instead of the project venv. **Putting
`.venv\Scripts` on `PATH` is not enough** — the host only picks the project interpreter when the
venv is *activated*, i.e. when `VIRTUAL_ENV` is set.

*Fix — set both, which is exactly what `Activate.ps1` does:*

```powershell
$env:VIRTUAL_ENV = "$PWD\.venv"
$env:PATH        = "$PWD\.venv\Scripts;$env:PATH"
```

**Rule.** If a local run fails on an import, check *which interpreter the host started* before
touching requirements:

```powershell
.\.venv\Scripts\python.exe -c "import sys; print(sys.executable)"   # what you think you're running
```

### 6.3 Gotcha #2 — the host trusts the corporate proxy, the worker does not

*Symptom:* the host starts, storage works, the claim works — and only the outbound HTTPS call to
the external service fails with `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`.

*Cause:* the .NET host validates through SChannel (Windows cert store) and trusts the inspecting
agent's root; the Python worker uses `certifi`, which does not. This is not a host problem — any
venv-Python outbound HTTPS is affected, including the harnesses.

*Fix — a `sitecustomize.py` on `PYTHONPATH`, auto-loaded at interpreter startup, before `func start`:*

```python
# scripts/localshim/sitecustomize.py
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass
```

```powershell
$env:PYTHONPATH = "$PWD\scripts\localshim"
```

**Rule.** `--insecure` / disabled verification is not the fix. It changes the behaviour under
test, hides a real certificate problem, and has a way of surviving into a deployed configuration.

**Rule.** Distinguish the two auth failures — they look alike and have different fixes:
*request* TLS failing (`CERTIFICATE_VERIFY_FAILED` on the call) is a truststore problem;
*token acquisition* failing (`DefaultAzureCredential failed to retrieve a token`) is the proxy
blocking every credential chain leg, and the fix there is key auth from the local settings file.

### 6.4 Storage emulator: local state, remote intelligence

Run the storage emulator headlessly and point the app at it with
`"AzureWebJobsStorage": "UseDevelopmentStorage=true"`, so the ledger and diagnostics blobs stay
on the machine.

```gitignore
__azurite_db*.json
__blobstorage__/
__queuestorage__/
```

**Rule.** Be explicit in the README about **what stays remote**: an AI/extraction service has no
emulator, so local runs still call the real dev resource and still cost money and quota. Nobody
should discover that from a bill.

**Rule.** When the emulator crashes on startup against an existing store, point it at a fresh
empty folder rather than debugging stale local data.

### 6.5 One harness, both targets

**Rule.** Write **one** client harness whose `--base-url` *defaults to the local host*
(`http://localhost:7071`) and which takes `--base-url` + `--key` for the deployed app. Two
harnesses drift, and the local one becomes the one that lies.

```powershell
# local — no key needed
.\.venv\Scripts\python.exe scripts\test.py --file ".\samples\invoice1.pdf"
# deployed — same script, same flags
.\.venv\Scripts\python.exe scripts\test.py --base-url "https://<HOST>" --key "<KEY>" --file ".\samples\invoice1.pdf"
```

Design points worth copying:

- **A fresh GUID id per run by default**, so the idempotency claim never silently short-circuits
  a test you meant to run. `--source-id <guid>` re-posts a *specific* id, which is how you test
  the claim deliberately.
- **A `--bad-payload` mode** that exercises the validation path only — it must 400 before any
  state is touched, and that is worth a one-flag regression.
- **It imports the production rules modules** (§2.1), so its scorecard reports what the service
  actually decided rather than a second opinion.
- **Standard library only** for its own logic, so it runs anywhere without an install step.

### 6.6 What a local run can never prove

**Rule.** Write this list into the README. Every item is a class of bug that only appears after
deploy, and knowing the list is what turns "it worked locally" from an excuse into a diagnosis.

| Not covered locally | Why |
|---|---|
| Managed identity + RBAC | Locally you are your `az login` principal, with *your* roles. Production runs as the app's identity, with different ones. |
| Application settings | `local.settings.json` is never deployed; a key that exists locally may simply not exist in the cloud. |
| Packaging | `.funcignore` correctness only shows up at publish time. |
| Cold start, plan limits, platform timeouts | Not modelled by the local host at all. |
| The caller's timeout budget | The orchestrator's connector budget (§2.6) is a property of the caller, not of your host. |

### 6.7 One command to start

```powershell
# scripts/start-local.ps1 — run from the repo root
$env:VIRTUAL_ENV = "$PWD\.venv"                 # 6.2 — Core Tools picks the venv from THIS
$env:PATH        = "$PWD\.venv\Scripts;$env:PATH"
$env:PYTHONPATH  = "$PWD\scripts\localshim"     # 6.3 — sitecustomize.py TLS shim
Set-Location functionapp
func start
```

**Rule.** Commit that script. Every environment quirk above is invisible to the next person, and
a three-line script is cheaper than a three-paragraph troubleshooting entry they will not read
first.

---

## 7. Deployment

### 7.1 A "deploy" is several independent artifacts, and the order matters

Enumerate them explicitly; this project has five, and only the first is what people mean by
"deploy":

| # | Artifact | Deployed by |
|---|---|---|
| 1 | Function code | `func … publish --build remote` |
| 2 | **Service/prompt definition** | its own provisioning script — editing the JSON changes *nothing* live |
| 3 | Application settings | `az functionapp config appsettings set` |
| 4 | Identity + RBAC role assignments | one-time per environment, easy to forget in a new one |
| 5 | The orchestration flow (Power Automate / Logic App) | its own tooling, its own lifecycle |

**Rule — push the service definition BEFORE the code that depends on it.**

*Evidence:* a release renamed an extracted field (`invoice_date` → `invoice_date_extract`) and
added a twin. Deploying the code first would have made it read a field the live analyzer did not
yet emit — silently defaulting **every invoice to today's date** until the analyzer push landed.
The failure is silent by construction, because well-written code has a fallback for a missing field.

**Rule.** The full order, written down in a runbook and followed every time:

```
pytest green
  → commit                     (the regression stamp must describe a clean HEAD)
  → corpus regression          (PASS; writes out/regress/<sha>.json)
  → push the service definition (gate: refuses without the stamp)
  → publish the code
  → verify app settings + roles
  → smoke test the deployed endpoint
  → confirm the state-store row
```

**Rule.** Source-control push is independent of deploy. Say so, or someone will assume `git push`
shipped something.

### 7.2 Config does not deploy — verify the live app after every release

`local.settings.json` never leaves the machine. After every deploy, read back the settings that
actually exist:

```powershell
az functionapp config appsettings list -g $RG -n $APP `
  --query "[?contains(name,'AZURE_') || name=='FIELD_CONFIDENCE_THRESHOLD'].{name:name, value:value}" -o table
```

**Rule.** Keep the expected table in the runbook, so "verify settings" is a comparison rather
than a judgement call. And remember §2.7: a **code default is not the live value** — a constant
read from source said 600 s while the live app setting said 300 s, and the wrong number
invalidated the retry-interval math that prevents a silent-drop failure mode.

### 7.3 Identity and RBAC — the part a local run cannot simulate

**Rule.** List every role assignment the app needs, by resource, in the runbook. In a new
environment this is the single most common cause of "it worked in dev": the code is identical
and the role assignment is missing.

- data-plane role on the AI service (e.g. *Cognitive Services User*)
- data role on the state store (e.g. *Storage Table Data Contributor*)
- data role on the host/deployment storage when the platform is keyless (e.g. *Storage Blob Data Owner*)

**Rule.** Prefer **keyless** (managed identity) in the cloud, and set the client id explicitly
when several identities exist, so the credential chain cannot pick the wrong one.

**Rule — never let a local convenience setting reach the cloud app.** Adding a storage
*connection string* setting (including `UseDevelopmentStorage=true`) **overrides** keyless host
storage and breaks the app in a way that reads as a platform fault.

### 7.4 Remote build, and the version warning that is not a problem

**Rule.** Build on the platform (`--build remote`, or `scmDoBuildDuringDeployment: true` for the
IDE path). The platform reinstalls from `requirements.txt` server-side, so the venv is never
shipped and a native wheel is built for the *target* OS rather than yours.

**Rule.** A local/remote version-mismatch warning under remote build is **noise** — packages are
built on the platform against the app's configured runtime, not against yours. Never "fix" a
warning by changing the platform runtime; decide the runtime on its own merits.

**Rule — and the reason this one is worth reading twice.** This section used to say the newer
minor "had no remote-build support at all on this hosting plan", and told you to pin deliberately
and write down why. The pin was correct *and it outlived its cause*: the platform gap was fixed
upstream on 2026-06-23, but the rule sat in four files unchallenged until 2026-08-20, backed by a
**stale warning string in the build tool** that made the limitation look like it persisted. The
runtime was migrated that day with no functional change (611/611 corpus expectations green).

> **A platform-capability rule needs a "verified on" date and a re-check command, or it becomes
> folklore.** Re-check against the platform's own API — here
> `az functionapp list-flexconsumption-runtimes` — never against a tool's warning text. A CLI
> warning is a *claim about the past*; the control-plane API is the present.

**Rule.** Write down *where* a setting lives, not just its value. The runtime version on this
hosting plan lives under `functionAppConfig`, not `siteConfig`, so the obvious
`config set --python-version` succeeds and changes nothing. A setting that silently no-ops is
worse than one that errors.

**Rule.** Keep a packaging ignore file (`.funcignore`) and check it when the artifact grows:

```
__pycache__/
*.py[cod]
.venv/
.python_packages/
local.settings.json
local.settings.json.template
README.md
.vscode/
tests/
```

### 7.5 The deployment gotchas that cost the most time

| Symptom | What it actually is | What to do |
|---|---|---|
| `Unable to connect to Azure. Make sure you have the az CLI … logged in` — while `az account show` succeeds | The deploy tool shells out to a **raw child `az` process** for an ARM token. A shell-function/profile wrapper lives in *your* shell and is invisible to children (§5.2). And `az account show` only reads the local token cache, so it proves nothing. | Put a real `.cmd` shim on `PATH`. Verify with `az account get-access-token --query expiresOn -o tsv` — the only check that exercises the network. Pre-warming the token cache (~1 h) also works. |
| `Can't find app with name "<app>"` — the app plainly exists | A dropped TLS handshake on the tool's app-lookup call, which has **no retry**. Measured at 1 success in 8 during one bad window, and first-try success an hour earlier. | Retry in a loop (≤8). Publish is idempotent, so a retry after a partial failure is safe. **This message means "try again", not "wrong name".** |
| A TLS error printed *after* `The deployment was successful!`, and a non-zero exit code | The tool's final step fetches invoke URLs through the proxy. The deploy already landed. | **Read the log, not the exit code.** |
| Publish fails validation with `InaccessibleStorageException` naming the **old** storage account — after an ARM readback confirms the new one | The deployment (Kudu/Legion) environment is provisioned with the site's storage config and does not re-read it on a plain restart. | Full **stop → wait ~20 s → start → wait ~60 s**, then publish. A restart is *not* enough. |
| `<app>.azurewebsites.net` does not resolve | Flex Consumption apps get a **hashed** default hostname. | Read `defaultHostName` off the site resource. A DNS failure here is not evidence of an outage. |
| Every function key returns 401 | Replacing the host storage account **invalidates every previously minted key**. | Re-read the key after any host-storage change; treat all older keys as dead. |
| Deployed app 502s on every request while local works | Data-plane role missing for the app's identity on the AI service. (A 500 points at the state store's role instead.) | Fix the role assignment (§7.3), not the code. |

**Rule.** When the primary tool's auth path is broken by the network, have a **documented
alternate deploy path** rather than re-running the failing command. Here: zip the payload
honouring `.funcignore`, get a token from the working CLI path, and `POST` the zip to the site's
SCM `…/api/publish?RemoteBuild=true` over a different TLS stack (PowerShell `Invoke-WebRequest`,
which uses SChannel), then poll the returned deployment URL to completion. That path shipped a
release on a day the normal one could not authenticate at all.

**Rule.** During an outage window, **probe before retrying**. A trivial request to the management
endpoint returning *any* HTTP status means transport is fine and the problem is elsewhere; a
connection reset means stop hammering it.

### 7.6 A deploy is not finished until you have read the state store

**Rule.** The smoke test is part of the deploy, not an optional follow-up, and it runs the same
harness as local (§6.5) with `--base-url`/`--key`:

1. **Validation path** (`--bad-payload`) → expect 400, with no state touched.
2. **A real input** → expect 200, a decision, and no write error in the response.
3. **A known edge case** the release was about → expect the new behaviour specifically.
4. **Re-post the same source id** → expect the idempotency response.
5. **Query the state store** and confirm the rows are there with the expected status.

Step 5 is the one people skip, and it is the one that catches a silent persistence failure —
the response can look perfect while the ledger write is failing.

### 7.7 Escapes, and when they are legitimate

- The prod-push gate's `--force` is for a **rollback or emergency only** — never to make a red
  corpus go away (§4.3).
- Publish is idempotent; retrying is always safe.
- Keep the runbook's placeholders (`<APP>`, `<RG>`, `<HOST>`, `<KEY>`) as literal placeholders,
  and never paste a real key into a command line, a shell history, or a doc. Stage it in an
  environment variable and delete any file you put it in.

---

## 8. Documentation and durable memory

Four files, four distinct jobs. Keeping them separate is what stops any of them from rotting.

| File | Job | Rule |
|---|---|---|
| `CLAUDE.md` | **Rules and behaviour** for contributors and AI assistants. Tracked. | Keep it short and imperative. No history, no war stories — those go to troubleshooting. |
| `CLAUDE.local.md` | Machine-specific paths, local shims, revert steps. **Gitignored.** | Anything with a username or a machine path lands here by default. |
| `docs/ai/troubleshooting.md` | **Confirmed** mistakes, failed commands, environment issues, verified fixes. | Add only what actually happened *and* was verified *and* is likely to recur. Never guesses, never secrets. Read it **before** starting debugging. |
| `docs/ai/open-defects.md` | Defects found and deliberately **not** fixed. | One row per deferred defect with a **measured rate**. When fixed, *move it to Resolved with the sha* — don't delete it, so a future regression on the same document is recognisable. |

**Rule.** For any multi-session effort, keep a **living plan tracker** (`docs/ai/<topic>-plan.md`)
with stages, a checklist, and a results section. When a hypothesis is disproved, **mark the old
claim withdrawn in place rather than deleting it** — the disproof is the valuable part, and
deleting it invites someone to re-derive the same wrong conclusion.

**Rule.** Deliverables stay local. Write HTML/Markdown reports into `docs/` and reference them by
path; do not publish project material to externally hosted pages.

**Rule.** Never commit secrets — keys, tokens, connection strings, SAS URLs, function keys — to
source, tests, docs, sample payloads, or screenshots. Use `YOUR_API_KEY`-style placeholders in
examples, and keep real values in a gitignored settings file or the platform secret store. Check
the diff for secret-like values before every commit.

**Rule.** Gitignore an assistant's local permission allowlist. It accumulates session-scoped temp
paths and machine paths, and it re-enters history every time it is committed.

---

## 9. Standing rules for working with an AI assistant on the codebase

These belong in the new project's `CLAUDE.md` almost verbatim.

1. **State the plan as verifiable steps** before multi-step work: `1. [step] → verify: [check]`.
2. **Use the project venv path explicitly** in every command (§5.1).
3. **Check `docs/ai/troubleshooting.md` first** for any debugging, environment, or dependency work.
4. **Find the failing layer before editing.** Command syntax → working directory → interpreter →
   venv → dependency → env var → path → external service → application code → test expectation.
   Do not fix a symptom before confirming the cause.
5. **Justify every new dependency**; prefer the standard library; install into the venv; verify
   with `pip show`; update the requirements file.
6. **The corpus must track every change.** A bug fix with a reproducing input **adds** that input
   to the corpus in the same change. A requirement change **backfills** the new field into *every*
   sidecar, each value verified against its source document.
7. **Adding or dropping an asserted field is the user's decision.** Present the evidence and wait.
   Never quietly drop an assertion to make a suite green.
8. **If it is unclear whether the corpus needs updating, ask.** Guessing is expensive both ways:
   a skipped update leaves a hole in the net; an invented one asserts a value nobody checked.
9. **Report faithfully.** If tests fail, show the output. If a step was skipped, say so.
10. **Review the diff before finishing.** Every changed line must trace to the request.

---

## 10. New-project checklist

**Day one**

- [ ] `py -m venv .venv`; install requirements with the explicit interpreter path; verify with `pip show`.
- [ ] Repo shape from §2.1; deployable folder isolated; `pytest.ini` scoping collection to `tests/`.
- [ ] `.gitignore`: venv, real settings file, samples, outputs, emulator files, local notes, assistant permission files. Add the corpus ignore-with-negation pattern (§4.2) the moment a corpus exists.
- [ ] `local.settings.json.template` committed with **empty** secret values; the real file gitignored.
- [ ] `CLAUDE.md` + `CLAUDE.local.md` + `docs/ai/troubleshooting.md` + `docs/ai/open-defects.md` created from §11.
- [ ] TLS/truststore bootstrap in every outbound script if the network inspects TLS (§5.2).
- [ ] **`func start` runs and answers one request before the app does anything real** — the local loop is worth more early than late (§6).
- [ ] `scripts/start-local.ps1` committed (venv activation + TLS shim + `func start`) (§6.7).
- [ ] Storage emulator running, `AzureWebJobsStorage=UseDevelopmentStorage=true`, emulator files gitignored (§6.4).

**Before the first external-service call ships**

- [ ] Adapter / parsing / rules / orchestrator split (§2.2), rules module standard-library only.
- [ ] Single-source-of-truth constants + a `POLICY_VERSION` stamped on every persisted row.
- [ ] Claim-before-work, validate-before-claim, release-on-failure (§2.4).
- [ ] Raw response persisted per run as evidence, best-effort (§2.5).
- [ ] Call timeout derived from the caller's budget (§2.6).
- [ ] `RUN_SUMMARY` line + one alert built on it (§2.8).

**Before the first deploy**

- [ ] One harness, local by default, `--base-url`/`--key` for deployed (§6.5).
- [ ] The "what local cannot prove" list written into the README (§6.6).
- [ ] Every deployable artifact enumerated, with the **definition-before-code** order written down (§7.1).
- [ ] Runbook step that reads back the **live** app settings, with the expected table (§7.2).
- [ ] Every role assignment listed by resource; keyless where the platform supports it (§7.3).
- [ ] Remote build on; runtime version pinned **with the reason recorded**; `.funcignore` reviewed (§7.4).
- [ ] Smoke-test sequence ending in a **state-store read**, not just a 200 (§7.6).

**Before the first prompt/definition edit ships**

- [ ] Tier A contract tests over the definition files, derived from the code (§4.1).
- [ ] A golden corpus with tracked sidecars and gitignored inputs (§4.2).
- [ ] Content-addressed cache + two-tier pre-commit hook + installer (§4.3).
- [ ] Prod-push gate requiring a green stamp for a clean HEAD (§4.3).
- [ ] Written into `CLAUDE.md`: n ≥ 12, concurrent controls, never weaken the net (§4.4–§4.5).

---

## 11. Skeletons to copy

### `CLAUDE.md` outline

```markdown
# CLAUDE.md
Behavioral guidelines for this project. Platform: <OS/shell/language>.
Tradeoff: these bias toward caution over speed. For trivial tasks, use judgment.

## Project Context        # venv path + the exact command forms to use
## Related Project Memory Files   # troubleshooting, open-defects, playbook, local notes
## Standard Commands      # install / test / run, all with the explicit interpreter
## <Service> Changes and the Regression Corpus   # the net, and the rules that protect it
## Dependency Rules       # justify, prefer stdlib, install to venv, verify, record
## Secrets and Configuration     # placeholders only; where real values live
## 1 Think Before Coding · 2 Simplicity First · 3 Surgical Changes
## 4 Goal-Driven Execution · 5 Learn From Mistakes · 6 Debugging Discipline
## 7 Communication Style · 8 Completion Checklist
```

### Troubleshooting entry

```markdown
## <Symptom, as it will be searched for — the error string or the wrong behaviour>

**What happened (YYYY-MM-DD):** <observation, with measured rates>
**Cause:** <the layer that actually failed, and why>
**Fix:** <the exact command or diff that was verified to work>
**Rule:** <the transferable one-liner>
```

### Open-defect row

```markdown
| # | ID | Defect | Measured rate | Cost today | Fix shape |
|---|----|--------|---------------|------------|-----------|
| 7 | B4 | <what is wrong, on which document> | 5/12 | <impact if never fixed> | <code-side / prompt / config> |
```

### Self-correction format (use it in the moment, then promote it)

```markdown
Mistake:  [what was wrong]
Cause:    [why it happened]
Correction: [what to do instead]
Reusable lesson: [CLAUDE.md rule? troubleshooting entry? or temporary?]
```

---

## Appendix — the twelve most expensive lessons, compressed

1. An analyzer/prompt version is **confounded with wall-clock time**; only a concurrent control
   can attribute a regression. Two correct edits were reverted for want of one.
2. A **green cached suite** proves the cache is intact, not that the system is stable.
3. **n = 5 cannot attribute anything.** n ≥ 12 per arm.
4. **Correlated twin failure** defeats agreement-based confidence boosts.
5. **Prompt edits have unbounded blast radius** across other fields; code-side fixes don't.
6. **Silent defaults corrupt data invisibly.** Repeatedly needing to remove an assertion *is* the
   bug report.
7. A **code default is not the live value**; query the deployed app.
8. The **tracked hook is not the hook that runs**; re-install after editing.
9. Redirected stdout on Windows is **cp1252**, and it will bury a real failure under a traceback.
10. Persisting the **raw response of every run** is what made all of the above knowable at all.
11. **Deploy the service definition before the code that reads it.** The reverse order fails *silently*, because good code has a fallback for a missing field.
12. **"Can't find app with name X" means retry.** A deploy tool's auth and lookup calls run in a child process with no retry — the message describes the transport, not your app.
