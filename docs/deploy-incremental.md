# Incremental deploy — the runbook for a code change

Every release after the first one. [`deploy-to-azure.md`](deploy-to-azure.md)
builds the environment; this builds nothing and changes exactly one of its seven
artifacts — **#5, the function code**.

> **When this is the wrong document.** If the Entra registration, the SharePoint
> column index, the Azure resources, the RBAC assignments, the Key Vault refresh
> token or the Power Automate flow is missing or being changed, go to
> [`deploy-to-azure.md`](deploy-to-azure.md) §0 and work the artifact you need.
> This runbook assumes the other six exist and are correct.

**Read §0 before anything else.** The most common outcome of that section is that
you do not deploy at all — three of the knobs people most often want to change
cannot be deployed, because they live in the flow's request body.

---

## 0. Does this change need a deploy at all?

| What changed | Where it actually lives | Publish? |
|---|---|---|
| `batchSize` · `giveUpDays` · `stallMinutes` · `printerShareId` · `printFormat` · `library` · `folder` · `sharepointHostname` · `sharepointSitePath` | the flow's HTTP request body — **the only place** | **no** |
| How often the pipeline runs | the flow's Recurrence trigger | **no** |
| `PRINT_BUSINESS_TZ` · `PRINT_BUDGET_SECONDS` · `GRAPH_TIMEOUT_SECONDS` · `PRINT_RASTER_DPI` · `PRINT_RASTER_MAX_BYTES` | Function App application settings | **no** — §2 only |
| Anything under `functionapp/` — code, `host.json`, `requirements.txt` | the published artifact | **yes** — this runbook |
| `tests/` · `scripts/` · `docs/` · `CLAUDE.md` · `README.md` | the repository only | **never ships** |
| A SharePoint column name or type | SharePoint, and possibly the constants in `print_policy.py` | only if the constants moved |

Three consequences worth having in your head before you start:

> **`git push` ships nothing.** Source control and deployment are independent
> acts ([`ai/project-playbook.md`](ai/project-playbook.md) §7.1). A merged pull
> request has changed nothing in Azure.

> **Publishing runs from `functionapp/`**, so `tests/`, `scripts/` and `docs/`
> are outside the artifact by construction. `.funcignore` is what keeps
> `local.settings.json` out — the one that matters, because it holds a refresh
> token.

> **A leftover `PRINT_BATCH_SIZE`, `PRINT_GIVE_UP_DAYS`, `PRINT_STALL_MINUTES`
> or `PRINT_MAX_RETRIES` app setting does nothing at all.** The app-setting
> fallback for those was removed deliberately ([`design.md`](design.md) §5.8), so
> when the pacing is not what you expected, the flow body is the only place to
> look. Do not "fix" it in app settings; you will change nothing and believe you
> did.

---

## 1. Gates — before anything moves

```powershell
.\.venv\Scripts\python.exe -m pytest        # must be GREEN
git status                                  # know what you are shipping
git log --oneline -1                        # the sha that is about to be live
```

**Red means stop.** Never weaken a test to make a release pass; the offline suite
is the only thing standing between a refactor and a silent behaviour change.

**Commit first.** Rollback in §8 is `git checkout <GOOD_SHA>`, which needs a sha
that describes what is deployed. Publishing a dirty tree is legal and sometimes
right for a hotfix — but then nothing in the repository names what is running,
so write down what you did.

Confirm the CLI reaches Azure, and which subscription it is pointed at:

```powershell
az account show --query "{name:name, id:id}" -o table   # dev-Document Intelligence
az group list --query "[].name" -o tsv                  # must NOT be a certificate error
```

> **Do not run `az upgrade` during a deploy.** The CLI will offer it; decline. It
> replaces the exact interpreter the truststore wrapper invokes by absolute path,
> and a half-finished upgrade leaves you with no working CLI mid-release. Upgrade
> afterwards, deliberately. Full note in [`deploy-to-azure.md`](deploy-to-azure.md) §0.

---

## 2. Application settings — only if the change needs one

**Skip this section unless the diff adds, renames or retunes a setting.**

A code change that reads a **new** setting needs the setting live **before** the
code that reads it. Otherwise the first run after publish takes a code default,
silently, and a code default is not the live value — the failure that
[`ai/project-playbook.md`](ai/project-playbook.md) §7.1 exists to prevent.

```powershell
$RG  = "rg-noble-print"
$APP = "func-noble-print"

az functionapp config appsettings set --resource-group $RG --name $APP --settings `
    "PRINT_RASTER_DPI=300"
```

Removing one:

```powershell
az functionapp config appsettings delete --resource-group $RG --name $APP `
    --setting-names PRINT_MAX_RETRIES
```

> **Do this inside §3's window, not before it.** Whether a settings write bounces
> the app is not recorded anywhere in this repository and has not been verified
> here — so the runbook simply never depends on the answer. With the flow already
> stopped, a restart at this moment costs nothing either way.

**`PRINT_REFRESH_TOKEN` must never be set in Azure.** It is a local-development
escape hatch that works in production, which is exactly the danger.

---

## 3. Quiet the pipeline

**Turn the flow off, then wait for any run already executing to finish.**

Stopping a flow prevents the *next* trigger; it does not kill a run in progress.
Read the recurrence off the trigger, then confirm nothing is mid-invocation — the
newest `RUN_SUMMARY` should be older than `PRINT_BUDGET_SECONDS` (90 s):

```kusto
traces
| where timestamp > ago(30m) and message startswith "RUN_SUMMARY"
| project timestamp, message
| order by timestamp desc
```

This is not ceremony. Publishing replaces the running worker, and the pipeline
has two windows in which a replaced worker does real damage. Both are live,
because the one flow calls Submit and Status in the same run.

### Why — the Submit window

`printing.sender.send` returns only after the print job has been **created and
started** (`universal_print.py:294` — step 4 of the four-step submit is
`POST …/jobs/{job}/start`). Paper is on its way. The PATCH that writes
`Print_JobId` happens *after* that, at `function_app.py:659-661`.

That PATCH is guarded against raising, because a failure leaves `PRINT_PENDING`
with an empty `Print_JobId` — which this design defines as a crashed submission
([`design.md`](design.md) §5.3), so Status requeues it and **the document prints
a second time**. The guard cannot catch the worker being replaced, and that path
logs no `ERROR`, because nothing survives to log one.

| A publish kills Submit… | Cost |
|---|---|
| before the claim | nothing — the file is untouched |
| after the claim, before the job is sent | benign — `PRINT_PENDING` with no job id, which Status requeues; prints once, one boundary late |
| **between the job starting and the `Print_JobId` write** | **a duplicate print, with no warning anywhere** |
| after the `Print_JobId` write | nothing — the row is complete |

### Why — the Status window

Status cancels a stalled job **before** it requeues the file
(`function_app.py:1090-1112`, and rule 2 in `CLAUDE.md`). A worker replaced
between those two steps leaves the job cancelled and the row still
`PRINT_PENDING`. The next run reads that job as `canceled`, and a cancelled job
maps to `PRINT_FAILED` — which is terminal. Nothing retries it; a human resets
the row.

So this window does not duplicate paper. It **strands a file that was one step
away from being retried**, and the only way back is by hand.

> Status's other branches are safe. A give-up cancels and then writes
> `PRINT_FAILED`, so a kill between them reaches the same state on the next run;
> a completion is a single write with nothing before it.

---

## 4. Publish

```powershell
.\.venv\Scripts\python.exe -m pytest        # green, again, on what you are shipping

# func shells out to the RAW az.cmd for an ARM token and cannot refresh it over
# the network through the inspecting proxy. Pre-warm the cache through the
# truststore wrapper first, or publish fails with "Unable to connect to Azure".
& 'C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe' -B "$env:LOCALAPPDATA\az-truststore\azrun.py" account get-access-token --output none

cd functionapp
func azure functionapp publish $APP --build remote
cd ..
```

**Publish is idempotent.** Retrying after any failure is always safe, and the two
most common failures are not failures at all:

| Symptom | What it is | Do |
|---|---|---|
| `Can't find app with name "func-noble-print"` | A dropped TLS handshake on the tool's lookup call, which has no retry | **Retry**, up to ~8 times. This means "try again", not "wrong name" |
| A TLS error *after* `The deployment was successful!`, non-zero exit | The final invoke-URL fetch failed. The deploy already landed | **Read the log, not the exit code** |

The fuller table, including the SCM 401/403 case, is in
[`deploy-to-azure.md`](deploy-to-azure.md) §7.

> **Remote build re-resolves `requirements.txt` on the platform.** That is why
> `pypdfium2` is pinned exactly at `5.13.0` and is the only pinned dependency: a
> range would let a later publish of the *same commit* install a pdfium the
> offline suite never ran against, and the only guard is a byte-exact output hash
> in `tests/test_printing.py`. The pin is also what makes re-publishing an old
> sha (§8) reproducible rather than approximate.

---

## 5. Verify the deploy landed

Four read-backs. Each catches a failure that otherwise looks exactly like
success.

**1. The right functions are indexed.**

```powershell
az functionapp function list --resource-group $RG --name $APP --query "[].name" -o tsv
# expect exactly three:
#   check_printer_health, poll_print_status, submit_print_jobs
```

Fewer than three means the worker failed to index the app — usually a missing
import or a `requirements.txt` problem — and the publish reported success anyway.
A `resubmit_print_jobs` means an old build is still deployed. If your change
*added* a route, update this expectation here and in
[`deploy-to-azure.md`](deploy-to-azure.md) §7.

**2. Settings are what you think.** Publishing never carries application
settings, so this is where a skipped §2 shows up:

```powershell
az functionapp config appsettings list --resource-group $RG --name $APP `
    --query "[?starts_with(name,'PRINT_') || starts_with(name,'GRAPH_') || name=='KEY_VAULT_URI'].{name:name,value:value}" `
    -o table
```

Compare against the expected table in [`deploy-to-azure.md`](deploy-to-azure.md) §8.

**3. Sampling is still off.**

```powershell
az functionapp config appsettings list --resource-group $RG --name $APP `
    --query "length([?contains(name,'Sampling')])" -o tsv     # 0 is the pass
```

`host.json` is the authority and ships with every publish, so this can only
regress through an app setting that overrides it. It counts rather than filters
because a dropped `az` handshake and a genuinely missing setting otherwise look
identical.

**4. Telemetry is still wired.**

```powershell
az functionapp config appsettings list --resource-group $RG --name $APP `
    --query "length([?name=='APPLICATIONINSIGHTS_CONNECTION_STRING'])" -o tsv   # 1 is the pass
```

Then capture the host and a key for §6:

```powershell
$HOST_NAME = az functionapp show --resource-group $RG --name $APP --query defaultHostName -o tsv
$KEY  = az functionapp keys list --resource-group $RG --name $APP --query "functionKeys.default" -o tsv
$BASE = "https://$HOST_NAME"
```

> **Read the key; do not reuse a stored one.** What this repository documents is
> that replacing the host storage account invalidates every previously minted key
> ([`ai/project-playbook.md`](ai/project-playbook.md) §7.5). Whether an ordinary
> publish preserves keys is *not* documented here, so this runbook does not
> assume it — re-reading costs one command and removes the question. Flex
> Consumption also hashes the default hostname, so always read `defaultHostName`
> rather than constructing it.

---

## 6. Smoke test — the deploy is not finished until you read the columns

The same harness as local, pointed at the deployed app. **Run all of it, every
release.** A response can look perfect while the write behind it is failing, and
step 5 is the only thing that catches that.

```powershell
$SITE  = @("--hostname", "noblehomes.sharepoint.com", "--site-path", "/sites/PM")
$QUEUE = @("--library", "AI_DropBox_V2026", "--folder", "/Backup/Invoice")
$SHARE = "5f488e73-ab80-4a6b-a60a-a0f883e17e2e"      # perishable -- see below
$FMT   = @("--print-format", "image/pwg-raster")     # must match what the flow sends

# 1. Validation path: must 400, must touch nothing.
.\.venv\Scripts\python.exe scripts\test.py badpayload --base-url $BASE --key $KEY

# 2. Dry run: resolves the site, the library, the five internal column names,
#    the printer's real capabilities, and WHICH PROFILE would run. No paper.
.\.venv\Scripts\python.exe scripts\test.py dryrun --base-url $BASE --key $KEY `
    @SITE @QUEUE --printer-share-id $SHARE @FMT

# 3. Exactly one real file.
.\.venv\Scripts\python.exe scripts\test.py submit --base-url $BASE --key $KEY `
    @SITE @QUEUE --printer-share-id $SHARE @FMT --batch-size 1

# 4. Once the page is out, mark it complete.
.\.venv\Scripts\python.exe scripts\test.py status --base-url $BASE --key $KEY `
    @SITE @QUEUE
```

**5. Read the five columns back in the library.** This is the step people skip.

| After step 3 | After step 4 |
|---|---|
| `Print_Status` = `PRINT_PENDING` | `PRINT_COMPLETED` |
| `Printer_Name` = the share id | unchanged |
| `Print_JobId` = a short number | unchanged |
| `Print_Message` = whatever it held before | `printed on YYYY-MM-DD HH:MM:SS` |

**6. Read the paper.** A job Graph calls `completed` can still come out cropped
or scaled; only the sheet shows it.

**7. Confirm telemetry arrived.**

```kusto
traces
| where timestamp > ago(30m) and message startswith "PRINT_EVENT"
| project timestamp, message
```

**8. Exercise the change itself.** A release is smoke-tested against its own
diff, not only against the happy path. Add whichever step proves the thing you
actually changed — the edge case that motivated it, ideally the one that failed
before.

### Two notes on running this against a live queue

**You need a `PRINT_READY` file, and a live queue may have none.** Either drop a
PDF into `/Backup/Invoice` and set `Print_Status` to `PRINT_READY`, or reset one
already-completed row. `Print_Time` must be empty or in the past, or Submit will
refuse to claim it as not yet due.

**The share id above is perishable.** Deleting and re-creating a printer share
mints a new one, and the only symptom is a 404. If step 2 fails that way, read
the current id from *Universal Print → Printers → the printer → Overview*.

### Conditional — `verify_print_time.py`

Not part of every release. Run it when the change touches `Print_Time` handling,
or when the column has been recreated — the offline suite cannot prove the round
trip, because `FakeGraph` stores whatever it is handed.

```powershell
.\.venv\Scripts\python.exe scripts\verify_print_time.py @SITE --library "AI_DropBox_V2026"
```

> It talks to Graph **directly**, not through the deployed app — no `--base-url`,
> no `--key`. It needs `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID` and `KEY_VAULT_URI`
> in the same shell, or the equivalent in `functionapp/local.settings.json`.

---

## 7. Turn the pipeline back on

Re-enable the flow, then watch one full cycle. One run exercises all three
endpoints, so a single green cycle is real evidence.

```kusto
traces
| where timestamp > ago(1h) and message startswith "RUN_SUMMARY"
| project timestamp, message
| order by timestamp desc
```

**Two rows mean a publish window caught you.** Look for both in the library
before the next recurrence:

| What you see | What happened | What to do |
|---|---|---|
| `PRINT_PENDING` with an empty `Print_JobId` | Submit was killed between the claim and the job id write | **If the page came out**, set the row to `PRINT_COMPLETED` by hand *now* — otherwise Status requeues it and it prints again. If no page came out, leave it; Status will requeue it correctly |
| `PRINT_FAILED` whose `Print_Message` shows a cancel but no retry | Status was killed between cancelling the old job and requeuing the file | Reset the row to `PRINT_READY`. Nothing recovers a terminal row on its own |

> **One property of the flow shape works in your favour here.** Health runs
> first, so a badly broken release stops the cycle before any file is claimed.
> Do not lean on it — it holds only while the failure is one Health detects.

---

## 8. Rollback

**Turn the flow off first.** It is instant, it needs no deployment, and it costs
nothing: files sit at `PRINT_READY` and the library *is* the queue, so nothing is
lost. Diagnose afterwards.

```powershell
git checkout <GOOD_SHA>
cd functionapp; func azure functionapp publish $APP --build remote; cd ..
```

Two things about rolling back a release specifically:

- **A code rollback does not revert a §2 settings change.** If the release
  changed an application setting, put the old value back yourself — nothing in
  the publish does it for you.
- **The flow cannot stay off indefinitely.** The queue is safe for as long as you
  like, but the refresh token is only rotated *when the app runs*, so more than
  **90 days** off lets it expire and the first run afterwards 500s until
  `bootstrap_token.py` is re-run. See [`deploy-to-azure.md`](deploy-to-azure.md) §12.

---

## 9. When something fails

Failures specific to a re-release. Everything else is in
[`deploy-to-azure.md`](deploy-to-azure.md) §13 and
[`ai/troubleshooting.md`](ai/troubleshooting.md).

| Symptom | Cause | Fix |
|---|---|---|
| Fewer than three functions listed | The worker could not index the app — a missing import, or a dependency that did not install | Read the publish log for the pip step. Publish reports success regardless |
| `resubmit_print_jobs` in the list | An old build is deployed | Re-publish; that route was retired 2026-09-01 |
| `ModuleNotFoundError` at runtime, worked locally | Remote build did not install the package | Confirm it is in `functionapp/requirements.txt` and that publish used `--build remote` |
| Publish succeeded, new behaviour absent | Either an old build, or the change also needed a flow-body edit | §5 step 1, then re-read §0 — the change may not have been a code change at all |
| Works locally, 500 in Azure | Settings or roles, not code — a local run cannot exercise either | §5 step 2, then [`deploy-to-azure.md`](deploy-to-azure.md) §4 |
| A duplicate or stranded row right after a release | §3 was skipped, or a run was still in flight when publish landed | §7's table |
| The byte-hash test in `test_printing.py` fails after a dependency bump | pdfium changed its rasterization | Print one page ([`e2e-testing.md`](e2e-testing.md) Part A) and confirm the device accepts it **before** updating `FIXTURE_SHA256_300DPI` |
| Counts in the weekly workbook look low | Adaptive sampling got enabled | §5 step 3 |
| `ConnectionResetError [WinError 10054]` from any `az` command | Local TLS interception on the workstation, not Azure | **Retry.** The profile wrapper already retries five times and is silent on success |

Anything not on this list belongs in
[`ai/troubleshooting.md`](ai/troubleshooting.md) once the cause is confirmed and
the fix verified.

---

## Appendix — one-page checklist

```
[ ]  0  Confirmed this actually needs a deploy -- not a flow-body or app-setting
        change (three of the most-changed knobs are flow-body only)
[ ]  1  pytest GREEN; committed; sha noted; az reaches Azure; right subscription
[ ]  1  did NOT run az upgrade
[ ]  2  New/renamed app settings written BEFORE the code that reads them
[ ]  3  Flow turned OFF, and any in-flight run allowed to finish
        (RUN_SUMMARY older than 90 s) -- a publish mid-Submit can print twice,
        a publish mid-Status can strand a file terminally
[ ]  4  ARM token pre-warmed through the truststore wrapper; published with
        --build remote; read the LOG, not the exit code
[ ]  5  THREE functions listed; settings read BACK; sampling query = 0;
        App Insights count = 1; host and key RE-READ, not reused
[ ]  6  badpayload 400 · dryrun conversion is what the flow will run · one real
        file · READ THE COLUMNS · READ THE PAPER · PRINT_EVENT in App Insights
[ ]  6  A step that exercises THIS change specifically
[ ]  6  verify_print_time.py -- only if the change touched Print_Time
[ ]  7  Flow back ON; one full cycle watched; library checked for a PRINT_PENDING
        row with no job id, and a PRINT_FAILED row showing a cancel but no retry
[ ]  8  If rolled back: settings reverted by hand too -- the publish does not
```

---

## Related reading

| Document | What it is for |
|---|---|
| [`deploy-to-azure.md`](deploy-to-azure.md) | Building the environment. §7–§9 are the source of this runbook's commands; §10 is the flow |
| [`e2e-testing.md`](e2e-testing.md) | Proving the printer and the pipeline. Part A needs no Azure at all |
| [`timing.md`](timing.md) | Every clock, and who owns it — the authority on what needs a redeploy |
| [`design.md`](design.md) | §5.4 is the write matrix; §5.8 is why the tunables are not deployable |
| [`ai/project-playbook.md`](ai/project-playbook.md) | §7 is the deployment law this runbook applies |
| [`ai/troubleshooting.md`](ai/troubleshooting.md) | Confirmed mistakes and verified fixes. Read before debugging |
