# Troubleshooting

Confirmed mistakes, failed commands, environment issues and **verified** fixes.
Read this before starting any debugging or environment work.

Only add an entry when the issue actually happened, the fix was verified, and the
lesson is likely to recur. No guesses, no secrets, no one-off errors.

Format:

```markdown
## <Symptom, as it will be searched for -- the error string or the wrong behaviour>

**What happened (YYYY-MM-DD):** <observation, with measured rates where relevant>
**Cause:** <the layer that actually failed, and why>
**Fix:** <the exact command or diff that was verified to work>
**Rule:** <the transferable one-liner>
```

---

## `RecursionError: maximum recursion depth exceeded` when faking the clock

**What happened (2026-08-30):** a test patched `graph_auth.time.time` with a
lambda that itself called `time.time()`. Because `graph_auth.time` *is* the
stdlib `time` module, the patch replaced the function the lambda then called.

**Cause:** test code, not application code. Patching a module attribute that the
replacement closes over is self-referential.

**Fix:** capture the real callable before patching.

```python
real_time = time.time
jumped = real_time() + 3600
monkeypatch.setattr(graph_auth.time, "time", lambda: jumped)
```

**Rule:** when monkeypatching a function you also need to call, bind the original
to a local **before** the patch.

---

## An out-of-range request value returned 500 instead of 400

**What happened (2026-08-30):** `{"batchSize": 99}` produced a 500. `print_policy`
raises a plain `ValueError` for an out-of-range value, but the route only caught
`BadRequest`, so it fell through to the generic handler.

**Cause:** `BadRequest` subclasses `ValueError`, which reads as though the reverse
were also true. It is not: a plain `ValueError` is not a `BadRequest`.

**Fix:** `function_app._tunable()` wraps the resolvers and translates `ValueError`
into `BadRequest`.

**Rule:** a validation helper that raises a *builtin* exception needs an explicit
translation at the boundary. Test the 400 path for every tunable, not just the
happy one.

---

## A bash heredoc mangled a Python file and wrote nothing

**What happened (2026-08-30):** writing `tests/fake_graph.py` through
`cat > file <<'EOF'` failed with `unexpected EOF while looking for matching
quote`, leaving no file at all.

**Cause:** shell quoting inside a large Python body.

**Fix:** use a real file-writing tool for Python sources; keep heredocs for short,
quote-free content.

**Rule:** already in the playbook (§5.3) — "writing backslash-heavy text through
a shell heredoc mangles it; write a real file with a file tool". It applies to
quotes as much as backslashes.

---

## `printed on ...` rendered UTC instead of Vancouver time

**What happened (2026-08-30):** a smoke check printed `21:23:23` rather than
`14:23:23`, with a warning that the time zone was unavailable.

**Cause:** `tzdata` was not installed in the interpreter being used. Windows has
no system IANA database, so `zoneinfo` cannot resolve `America/Vancouver`.

**Fix:** `tzdata` is in `functionapp/requirements.txt` for exactly this reason.
The fallback to UTC is deliberate — a reporting time zone must never fail a print
run — but it *is* a fallback, and the warning is the signal.

**Rule:** on Windows, any `zoneinfo` use needs `tzdata` as an explicit
dependency. Harmless on the Azure Linux host, required locally.

---

## A substring assertion counted three calls where one was made

**What happened (2026-08-30):** a test asserted
`len(graph.calls_to("/jobs", method="POST")) == 1` and saw 3.

**Cause:** `calls_to` matches a substring. `createUploadSession` and `/start`
both carry `/jobs/` in their URLs, so one submission produces three POSTs whose
URL contains `/jobs`.

**Fix:** `FakeGraph.created_jobs()`, which matches only URLs *ending* in `/jobs`.

**Rule:** when a test counts calls, match the endpoint precisely. A substring
that happens to be a URL *prefix* of other routes silently inflates the count --
and the failure looks like an application bug.

---

## Poll silently ignored most of its work

**What happened (2026-08-30):** Poll checked at most 15 pending jobs per run and
always chose the oldest. With more than 15 outstanding, the rest were never
checked; worse, a handful of long-running jobs occupied every slot run after run,
so a newer job that HAD completed was never marked and eventually aged past the
20-day window, where nothing touches it again.

**Cause:** reusing Submit's batching for Poll. Batching is right for Submit -- the
flow loops on `remainingReady` and each item costs a print job. Poll has neither
property: the requirement says "all files" and its flow makes a single call.

**Fix:** Poll iterates every job in the window, bounded only by the wall-clock
budget, and reports `uncheckedCount` when the budget trips.

**Rule:** do not copy a batching strategy between endpoints without checking
that the loop which makes it safe exists on both. A cap with no drain is silent
data loss, and ordering it oldest-first turns that into starvation.

---

## A fake that omitted a field taught everyone the field did not exist

**What happened (2026-08-30):** reading a real print job back out of Graph showed
`acknowledgedDateTime`, `isFetchable` and `errorCode`. The design had asserted for
its whole life that `printJob` "carries no timestamp", and `Print_Message` was
stamped with the moment Poll happened to run as a result.

**Cause:** `FakeGraph.add_job` built a job with `id` and `status` and nothing
else. Every one of the 344 tests then agreed, unanimously and offline, with an
incomplete picture of the resource. A fake is a claim about the API's shape, and
nothing in a suite that only talks to the fake can ever contradict it.

**Consequence:** not a bug -- the fallback behaviour was defensible -- but a
missed improvement that sat there unnoticed because no test could see the gap.
`printJob` still has no COMPLETION field, so the original claim was literally
true; it was just being used to justify something weaker than it supported.

**Fix:** model the fields on the fake, then prefer `acknowledgedDateTime` with a
fallback. `tests/test_acknowledged_time.py`.

**Rule:** when a fake stands in for an external resource, build it from the
resource's **documented property table**, not from the subset the current code
happens to read. And the first time you touch the real service, diff a real
response against the fake -- that is the only moment the omission is visible.

---

## A fixture recorded what the portal DISPLAYS, not what the API returns

**What happened (2026-08-30):** the first live run of
`scripts/live-printer-check.ps1 -DiagnoseOnly` contradicted the captured fixture
in two places:

| Field | Fixture said | API actually returns |
|---|---|---|
| `status.state` | `ready` | **`idle`** |
| `capabilities.contentTypes` | `["application/pdf"]` | **`["application/pdf", "application/oxps"]`** |

**Cause:** both values were transcribed from Azure portal screenshots.

- The portal renders the status as **"Ready"**. `printerProcessingState` is
  documented `unknown | idle | processing | stopped` — `ready` is not a member of
  it, so two tests asserted a string Graph can never return.
- The portal's *Properties > Printer defaults* page shows the **default** content
  type. `capabilities.contentTypes` is a **different field** — the full accepted
  list. The page does not label which is which, so one was read as the other.

**Consequence:** none at runtime — nothing branches on the printer state, and PDF
really is supported either way. The damage was to the tests: they pinned two
false facts and would have failed the moment anyone compared them with reality.

**Fix:** re-read both from the API, correct the fixture with a `_source` noting
the correction, and assert **membership** of `contentTypes` rather than an exact
list — membership is the actual requirement.

**Rule:** a portal screenshot is a rendering, not a response. When a fixture
claims to be API-shaped, capture it from the API. Where a portal page shows one
value and the resource has both a *capability* list and a *default*, assume you
are looking at the default until an API call says otherwise.

---

## A setting existed everywhere except in the code that reads it

**What happened (2026-08-30):** `GRAPH_TIMEOUT_SECONDS` was in
`local.settings.json.template`, in the deploy runbook's `appsettings set` block,
and in design §6.5's list of tunables. No module read it. Setting it to 60 in
Azure changed nothing, and nothing anywhere said so.

**Cause:** the constant (`graph_client.DEFAULT_TIMEOUT_SECONDS = 30.0`) was
written first and the env override was documented as if it followed. Every
review since read the doc, agreed it was right, and never checked the direction
of the dependency.

**Fix:** `graph_client.resolve_timeout_seconds()`, applied to the authenticated
client *and* to both unauthenticated paths, with tests for the range cases.

**Rule:** a settings template is a claim about the code, not a description of it.
Verify a config key the same way as any other API: `grep` for the *reader*, not
the declaration. The check is one command, and a key nothing reads is worse than
a missing one, because setting it looks like it worked.

---

## A default argument captured a value at import time

**What happened (2026-08-30):** `def download(url, timeout=graph_client.DEFAULT_TIMEOUT_SECONDS)`.
Once the timeout became configurable, this signature still resolved to the
constant, because a default expression is evaluated **once, when the module is
imported** -- long before any environment lookup could matter.

**Cause:** the idiom reads like "use the default", but it means "freeze today's
default into the signature".

**Fix:** `timeout: Optional[float] = None`, resolved inside the body.

**Rule:** never put a *configurable* value in a default argument. Use `None` and
resolve in the body, so the setting is read per call rather than per cold start.

---

## The Connectors blade shows "No rows to display"

**What happened (2026-08-30):** the Universal Print printer's Connectors blade
was empty, which looks like a broken registration -- a printer that is not
UP-ready and has no connector accepts jobs and silently never prints them.

**What it means: NOT DECIDABLE from the portal.** Universal Print talks to
printers over Mopria, so a **Universal Print-ready** printer registers *directly*
and needs no connector. A printer that is not UP-ready needs one. An empty list
is consistent with **both**, and the portal cannot separate them.

**The wrong answer I gave first, recorded so it is not repeated:** I argued from
the Overview blade -- *Last seen: 2 minutes ago*, *Status: Ready*, *Is accepting
jobs: Yes* -- that the printer must be talking to the service directly. That does
not follow. **A connector's heartbeat updates last-seen exactly as a native
printer's does**, so every one of those signals is produced by both cases. The
model is also a 2018 entry-level SOHO laser, which makes native UP firmware less
likely rather than more.

**What narrowing is available without printing:**
`scripts/live-printer-check.ps1 -DiagnoseOnly` lists connectors on the printer
**and across the tenant**. On 2026-08-30 both were **0**, which is stronger than
the blank blade -- it rules out a connector registered elsewhere. It still does
not decide the question.

**What actually settled it (2026-08-30):** running `live-printer-check.ps1`
without `-DiagnoseOnly`. It submitted a real job -- id `6`, `pending` ->
`processing` -> `completed` in about five seconds -- and a page came out, with 0
connectors anywhere in the tenant. **Verdict: Universal Print ready, registered
directly.** No Windows host in the path.

**Rule:** when two causes produce identical symptoms, no amount of reading the
symptoms harder will separate them -- find the observation that differs. Here
every portal signal (last seen, status, accepting jobs) and even the connector
count of 0 were produced equally by both causes; only paper in the tray was not.
And while the evidence only *fits* a conclusion rather than forcing it, say
"consistent with", not "confirmed".

---

## `Invoke-WebRequest` stopped a script with an interactive prompt

**What happened (2026-08-30):** halfway through the upload PUT,
`live-printer-check.ps1` printed *"Security Warning: Script Execution Risk ...
Do you want to continue?"* and waited for a keystroke.

**Cause:** Windows PowerShell 5.1's `Invoke-WebRequest` hands the response to the
Internet Explorer engine to build a parsed DOM, and warns before doing it. We
only ever wanted `$upload.StatusCode`.

**Fix:** `-UseBasicParsing`.

**Rule:** every `Invoke-WebRequest` in a script gets `-UseBasicParsing`. The
prompt is a harmless nuisance when a human is watching and an indefinite hang
when one is not -- which is exactly the case a diagnostic script gets used in
after it has been trusted once. The failure mode is invisible in the run where
you find it.

---

## `404 The printer share id '...' does not match any registered printers.`

**What happened (2026-08-31):** `.\scripts\live-printer-check.ps1 -DiagnoseOnly`
signed in cleanly and then 404'd on step 1. The portal at that moment showed the
printer **Ready**, **Shared**, **Is accepting jobs: Yes**, last seen one minute
ago -- so the device was demonstrably fine and the message, which names
*registered printers*, pointed at the wrong thing entirely.

**Cause:** the printer share had been deleted and re-created (Share date four
minutes before the failing run). **Re-sharing mints a new share id.** The printer
id was untouched -- `registeredDateTime` still read the original registration,
which is the tell: the device never re-registered, only the share was replaced.

**Fix:** read the current **Share Id** from *Universal Print > Printers > the
printer > Overview*, then replace the old id everywhere it was written down. It
was in eighteen places across six files, so sweep rather than guess:

```powershell
Select-String -Path .\README.md,.\docs\*.md,.\scripts\*.ps1,.\tests\*.py `
    -Pattern "<the retired share id>"
```

Expect hits in `scripts\live-printer-check.ps1` (the `-ShareId` default),
`README.md`, `docs\design.md`, `docs\live-test.md` and `docs\deploy-to-azure.md`.
The count must reach **zero**: a retired id has no business in a URL, a command,
a default or a field value.

**And the copies that are not in the repo:** the `printerShareId` in every Power
Automate flow body (Submit and Poll), plus anything in `CLAUDE.local.md`.
Those fail the same way and no test catches them.

Step 1 of `live-printer-check.ps1` now catches the 404 and lists the share ids
that do exist, so the next occurrence answers itself.

**Rule:** the share id is the **perishable** identifier and the printer id is the
durable one -- which is the opposite of how they are used, since the share id is
the one every caller passes on every request and the printer id is the one the
app resolves for itself at preflight. Treat a 404 on `/print/shares/{id}` as
"look the share id up again", never as "the printer is gone", and do not trust a
404 on that route as evidence of anything else -- it also voided the job
retention measurement that was counting days against a job created through the
retired share (see `docs/live-test.md`).

---

## `download: list item N has no downloadable driveItem (is it a folder?)` on an ordinary PDF

**What happened (2026-09-01):** the first live `submit` of a real file (e2e Part
B3) reported `submitted: 0, failed: 1` and wrote that message to
`Print_Message`. Item 2620 was not a folder — it was a 273,074-byte
`application/pdf` sitting in the right library. Probing every `PRINT_READY`
candidate in the folder showed the **same** result for all seven: the driveItem
came back with a `file` facet, a name and a size, and no download URL anywhere.

**Cause:** the `$select` on the driveItem GET, in `sharepoint.get_download_url`
since the initial commit. `@microsoft.graph.downloadUrl` is an OData
**annotation**, not a property, and a `$select` naming ordinary properties makes
the service return those properties and drop the annotations — including the one
named in the same `$select`. Measured against the live service:

| request | annotation returned |
|---|---|
| `?$select=id,name,size,file,@microsoft.graph.downloadUrl` | **no** |
| `?$select=@microsoft.graph.downloadUrl` (annotation alone) | yes — the select is ignored and the full item comes back |
| no `$select` | yes |
| `listItem?$expand=driveItem` | yes |

The error message was misleading rather than wrong: the code cannot tell an
absent URL from an unselectable one, and "is it a folder?" was the only
hypothesis it offered.

**Why no test caught it:** `FakeGraph._get_drive_item` ignored `$select` and
returned the URL unconditionally, so all 488 offline tests passed against a
request shape that could never download a byte. Same class of harness flaw as
F4 — the fake was more permissive than the service, in the one dimension that
mattered.

**Fix:** drop the `$select` (`functionapp/sharepoint.py`), and teach the fake to
model the real behaviour so the bug is reproducible offline. Verified live: the
adapter now returns a URL and downloads 273,074 bytes beginning `%PDF-`.
Pinned by `test_the_drive_item_request_does_not_select_away_the_download_url`,
which fails with the exact live error string when the `$select` is restored.

**Rule:** never put an OData annotation (`@microsoft.graph.*`) in a `$select`
alongside ordinary properties — either omit `$select` entirely or expand the
navigation property. And when a fake makes a query option a no-op, the tests
covering that call are asserting nothing about it.

---

## `ConnectionResetError: [WinError 10054]` downloading from `*.sharepoint.com`

**What happened (2026-09-01):** while verifying the fix above, the
pre-authenticated download failed twice and succeeded on the third identical
attempt — same URL, same process, seconds apart. Two resets out of three.

**Cause:** local TLS interception (the same Norton handshake reset the `az`
wrapper in the PowerShell profile retries five times for). It is a
**workstation** condition, not an Azure one, and it is unrelated to the
`$select` defect above — it strikes *after* a download URL is obtained.

**Fix (2026-09-01):** `graph_client.download_unauthenticated` now retries.
It made **one** attempt and had no retry loop of its own, so a local `submit`
landed a file at `PRINT_FAILED` with `download: ... Connection aborted` purely
from the local proxy — which is exactly what happened on the next B3 run. It now
mirrors the authenticated path: three attempts, exponential backoff with jitter,
and no retry for a non-retryable status (a 410 on an expired URL will expire
again). A document GET is idempotent, so this does not touch the "creating a
print job is not retryable" rule. Pinned by
`test_a_reset_connection_is_retried_rather_than_failing_the_file` and
`test_a_download_gives_up_after_the_attempt_limit`.

If it still exhausts all three attempts: the row is terminal, so reset it to
`PRINT_READY` before re-running.

**Rule:** a `download failed: Connection aborted` is the workstation, not the
pipeline. Distinguish it from a real download defect by whether an identical
retry succeeds — and retry the calls that are safe to retry, rather than letting
a claimed row die of someone else's TLS proxy.

---

## `403 The token does not have one or more required security scopes` on `createUploadSession`

**What happened (2026-09-01):** e2e B3, third attempt. Submit got *further* than
ever — the print job was created (id 27) and its document with it — and then
`POST /print/shares/{share}/jobs/27/documents/{doc}/createUploadSession` returned
403. The terminal truncated the body; the full text is above, and it names no
scope, which is what makes this expensive to diagnose from the message alone.

Decoding the live token's `scp` claim showed exactly what `graph_auth.SCOPES`
asks for: `Sites.ReadWrite.All`, `PrintJob.ReadWriteBasic`, `Printer.Read.All`,
`PrinterShare.ReadBasic.All`, `User.Read`.

**Cause:** `PrintJob.ReadWriteBasic` does not cover `createUploadSession`, and it
is the **only** one of the four print calls it does not cover. From the v1.0
permission tables:

| call | least privileged | is `ReadWriteBasic` accepted? |
|---|---|---|
| `POST /print/shares/{id}/jobs` | `PrintJob.ReadWriteBasic` | yes |
| `createUploadSession` | **`PrintJob.Create`** | **no** — `Create` or `ReadWrite` only |
| `POST .../jobs/{id}/start` | `PrintJob.Create` | yes |
| `POST /print/printers/{id}/jobs/{id}/cancel` | `PrintJob.ReadWriteBasic` | yes |

So the app could create a job it could never upload to. The failure lands
*after* the row is claimed and after a job exists on the printer, leaving an
orphan job at `paused`/`uploadPending` per attempt — harmless, since an unstarted
job never prints, but they accumulate.

**The evidence was already in the repo.** `scripts\live-print-test.ps1` — the
Part A bench script that prints paper — requests `PrintJob.Create` alongside
`PrintJob.ReadWriteBasic`. So did `samples\test-pwg-universal-print.ps1`. Only
the Function App's own scope list omitted it, which is why Part A passing said
nothing about whether Part B could upload.

**Fix:** add `PrintJob.Create` to `graph_auth.SCOPES`, then — and this is the
half that is easy to miss — **add the delegated permission in Entra, grant admin
consent, and re-run `scripts\bootstrap_token.py`.** A refresh token carries the
scopes consented when it was minted; editing the Python list alone changes
nothing about an existing token.

**Expect the app to go fully down in between.** `_redeem` asks for the whole of
`SCOPES` on every refresh, so the moment an unconsented scope is added to the
list, the refresh token stops redeeming at all:

```
AADSTS65001: The user or administrator has not consented to use the application
```

That is `AuthBootstrapRequired` on *every* endpoint, not a 403 on one call — it
is the expected state between editing `SCOPES` and finishing the consent, and it
clears the moment `bootstrap_token.py` succeeds. Do not read it as a second
defect, and do not roll the scope back to make it go away.

**Rule:** "Basic" scopes are not a subset relationship you can reason about —
check the permission table of **every** call in a sequence, not just the first
one. And when a bench script works and the app does not, diff their scope lists
before anything else.
