# Timing boundaries — every clock in the pipeline

Every timer, timeout, window and cadence this system depends on. Values read from
the **code**, not the other docs — a few of those have drifted.

### Legend — who controls each timer

| | Owner | Change it by | Redeploy? |
|---|---|---|---|
| 🎛️ | **Power Automate request body** | edit the flow's HTTP body (app setting is the fallback) | no |
| 🔧 | **App — setting** | Function App application setting | no |
| 🛠 | **App — code** | edit the constant in source | **yes** |
| 📄 | **App — host.json** | edit `functionapp/host.json` | **yes** |
| 🔁 | **Power Automate** | edit the flow's Recurrence trigger | no |
| 🔒 | **Azure / Microsoft** | **cannot be changed** | — |

---

## At a glance

| Owner | Count | Examples |
|---|---|---|
| 🔧 App — setting | 3 | `GRAPH_TIMEOUT_SECONDS`, `PRINT_BUDGET_SECONDS`, `PRINT_BUSINESS_TZ` |
| 🎛️ **Flow body**, app setting as fallback | 4 | `batchSize`, `stallMinutes`, `maxRetries`, `giveUpDays` |
| 🛠 App — code | 4 | retry attempts, backoff, `Retry-After` cap, token refresh margin |
| 📄 App — host.json | 1 | `functionTimeout` (currently unset) |
| 🔁 Power Automate | 3 | Flow A / B / D recurrences |
| 🔒 Azure / Microsoft | 6 | connector budget, token lifetimes, URL expiries, job retention |

---

## The chain that must not break

Four timers bound one call into this app. Each **must** undercut the next.

```
   GRAPH_TIMEOUT_SECONDS    30 s    one HTTP call        🔧 app
            <
   PRINT_BUDGET_SECONDS     90 s    one invocation       🔧 app
            <
   Power Automate HTTP     ~120 s   connector gives up   🔒 Azure — fixed
            <
   functionTimeout          30 min  platform kills it    📄 app (unset)
```

The 120 s row is the only one we cannot move, so it is the one everything else is
sized against. See note **[1]**.

---

## 1. Per-call and per-invocation

| Timer | Value | Range | Owner | Defined in |
|---|---|---|---|---|
| `GRAPH_TIMEOUT_SECONDS` | **30.0 s** | 1–300 | 🔧 setting | [graph_client.py:45](../functionapp/graph_client.py#L45) |
| `PRINT_BUDGET_SECONDS` | **90.0 s** | >0–600 | 🔧 setting | [print_policy.py:91](../functionapp/print_policy.py#L91) |
| Power Automate HTTP connector | **~120 s** | — | 🔒 **fixed** | not ours |
| `functionTimeout` | **30 min** (Flex Consumption default) | — | 📄 **unset** | [host.json](../functionapp/host.json) |

Notes **[1]**, **[2]**, **[3]**.

## 2. Retry and backoff — inside a single call

| Timer | Value | Owner | Defined in |
|---|---|---|---|
| `MAX_ATTEMPTS` | **3** | 🛠 code | [graph_client.py:48](../functionapp/graph_client.py#L48) |
| Backoff | `min(2^attempt, 8.0) + jitter(0–0.5 s)` | 🛠 code | [graph_client.py:143](../functionapp/graph_client.py#L143) |
| `MAX_RETRY_AFTER_SECONDS` | **20.0 s** | 🛠 code | [graph_client.py:52](../functionapp/graph_client.py#L52) |

Note **[4]**.

## 3. Identity

| Timer | Value | Owner | Defined in |
|---|---|---|---|
| Access token lifetime | **~60–90 min** | 🔒 **fixed** (Entra) | — |
| `REFRESH_MARGIN_SECONDS` | **300 s (5 min)** | 🛠 code | [graph_auth.py:57](../functionapp/graph_auth.py#L57) |
| **Refresh token lifetime** | **90 days** inactivity | 🔒 **fixed** (Entra) | — |

Note **[5]**.

## 4. Short-lived URLs

| Resource | Lifetime | Owner |
|---|---|---|
| SharePoint download URL | **"within minutes"** | 🔒 fixed |
| Universal Print upload session | service-defined | 🔒 fixed |

Both are pre-authenticated and must **not** carry an `Authorization` header —
Graph documents that adding one to the upload `PUT` "might result in an HTTP 401".
The download URL is fetched immediately before use and never cached, which is why
`get_download_url` and `download` are two calls made back to back.

## 5. The retry schedule — measured in UTC against `createdDateTime`

| Timer | Value | Range | Owner | Defined in |
|---|---|---|---|---|
| `PRINT_STALL_MINUTES` | **5 min** | 1–1440 | 🎛️ **request `stallMinutes`** + setting | [print_policy.py](../functionapp/print_policy.py) |
| `PRINT_MAX_RETRIES` | **10** | 1–20 | 🎛️ **request `maxRetries`** + setting | [print_policy.py](../functionapp/print_policy.py) |
| `PRINT_GIVE_UP_DAYS` | **10 days** | 1–365 | 🎛️ **request `giveUpDays`** + setting | [print_policy.py](../functionapp/print_policy.py) |
| `PRINT_BUSINESS_TZ` | `America/Vancouver` | — | 🔧 setting | [print_policy.py](../functionapp/print_policy.py) |

🎛️ means **the Power Automate flow body is the primary source**, ahead of the app
setting. That is deliberate: the pacing of the whole retry schedule can be retuned
by editing a flow, with no deploy and no app restart.

**Retry `n` falls DUE at file age `PRINT_STALL_MINUTES × (2ⁿ − 1)`** — Poll acts
on its next run, so observed times are quantised to Flow B's cadence:

| Retry | Due at | | Retry | Due at |
|---|---|---|---|---|
| 1 | 5 min | | 6 | 5h 15m |
| 2 | 15 min | | 7 | 10h 35m |
| 3 | 35 min | | 8 | 21h 15m |
| 4 | 1h 15m | | 9 | 1d 18h 35m |
| 5 | 2h 35m | | 10 | 3d 13h 15m |

- **5 minutes** is both the stall threshold — how long a *job* may sit without
  finishing — and the base of the schedule above. One knob rescales everything.
  Measured from `printJob.createdDateTime`, the job's own clock, so a SharePoint
  edit cannot reset it.
- **10 retries** bounds the **work**. Each requeue costs a render, a PDF→PWG
  conversion and an upload, so this is what stops an undeliverable document from
  consuming the printer's attention forever. Reached at about 3d 13h.
- **10 days** bounds the **waiting**. Past it, Poll cancels the outstanding job and
  writes `PRINT_FAILED` with a reason. Between the two bounds is a **grace period**
  of roughly 6½ days in which no new work is done but the last job stays live — so
  a printer that comes back still gets the document out.
- **The retry count is never stored.** The schema is four columns with no attempt
  counter; the count is derived from the file's age, and the current job's creation
  time says how many retries preceded it.
- **The polling cadence is the floor on the early boundaries.** Flow B runs every
  10 minutes, so the 5- and 15-minute boundaries fall inside one interval: a
  permanently stalled file produces **nine** requeue events, the first at ≈10 min,
  carrying retry numbers 1 and 3–10. Lowering `PRINT_STALL_MINUTES` below the
  recurrence does not speed the early retries up — only shortening the recurrence
  does.

- **`PRINT_BUSINESS_TZ` is display only** — the `printed on …` message. Schedule
  arithmetic never touches it, and the two paths are separate tested functions
  because mixing them is how daylight-saving bugs get in.

> **Three clocks, easily conflated:**
> `file age = now − listItem.createdDateTime` — never changes; drives the retry
> boundaries and the give-up test (R16). Our own writes would reset any clock based
> on `lastModifiedDateTime`, so the schedule would stall after one retry.
> `job age = now − printJob.createdDateTime` — the job's own clock; decides whether
> THIS attempt has stalled. A SharePoint edit cannot touch it.
> `attempt start` — the file's age when the current job was created. This is what
> says how many retries are already spent, and it is why no counter column exists.

## 6. Power Automate cadences

**No timer trigger exists anywhere in the function app** — both routes are
HTTP-triggered. Every row here is a flow edit with nothing to redeploy.

| Flow | Cadence | Owner |
|---|---|---|
| **A — Submit** | every **15 min**, `Do Until remainingReady == 0` **or 10 iterations** | 🔁 flow |
| **B — Poll** | every **10 min** | 🔁 flow |
| **D — Weekly digest** | **Mondays 07:00** | 🔁 flow |

Flow C — a daily Resubmit pass — **was deleted on 2026-09-01**; recovery is Flow
B's. Note **[6]** — Flow B's cadence is a correctness control, not a preference,
and it now also sets how quickly a stalled job is retried.

Flow B's body carries `stallMinutes`, `maxRetries` and `giveUpDays`, so the entire
retry schedule is a flow edit too.

## 7. External and unmeasured

| Timer | Value | Owner |
|---|---|---|
| **Universal Print job retention** | 🔴 **UNKNOWN — never measured** | 🔒 fixed |
| App Insights retention | **90 days free**, extendable to 730 at cost | 🔒 default, paid override |

Note **[7]**.

---

## Notes

**[1] The 120 s connector budget is why 90 exists.** A connector that has given up
never receives the response, so Flow A neither loops on `remainingReady` nor fires
its notify condition. The run is not reported as failed — it is not reported at all.

**[2] `PRINT_BUDGET_SECONDS` is anchored at request arrival**, not at the start of
the file loop: site resolution, printer preflight and the SharePoint query all
count against it. That was defect **S2** — before the fix, all of that work was
free. It is checked **before each file, never mid-file**, so it can never strand a
claimed-but-unsubmitted row.

**[3] `GRAPH_TIMEOUT_SECONDS` applies to every HTTP call** — authenticated Graph
calls *and* the two unauthenticated ones. It was defect **S4**: documented as a
tunable in three places while **no code read it**, so setting it did nothing,
silently.

**[4] ⚠️ Retry constants can violate the §"chain" from the inside.** Worst case for
a *single* Graph call:

```
   3 attempts × 30 s socket timeout   =   90 s
   2 sleeps   × 20 s Retry-After cap  =   40 s
                                          ─────
                                          130 s   for ONE call
```

That exceeds both the 90 s budget and the 120 s connector ceiling, and the budget
is only checked *between* files, so nothing interrupts it. Needs a throttled or
hanging Graph, so unlikely rather than impossible. Backoff jitter is deliberate —
without it a throttled batch retries in lockstep and trips the throttle again.

**[5] The refresh token is the operational timer to watch.** It rotates on every
redemption and Entra does not revoke the old one, so an app that runs regularly
never ages out — 90 days is an **inactivity** window. What kills it: a password
change, MFA reset, or admin revocation. **Password expiry alone does not.** When it
dies every call fails with `AuthBootstrapRequired` and recovery needs a human
running `scripts/bootstrap_token.py`. **Nothing alerts on this** — design §13.8
watches for *silence in the logs*, which reads identically to a quiet week.

**[6] Flow B's 10-minute cadence prevents UC-9.** Poll must run more often than
Universal Print discards finished jobs. If a job completes and is purged before
Poll sees it, Poll gets a 404, reads that as a stalled attempt, and requeues the
document — printing it twice. The mitigation is entirely the cadence, and the
cadence should be set from the measurement in **[7]**, which has not been taken.
Note the reprint now lands within minutes rather than after 72 hours, so there is
correspondingly less time to catch it by hand.

**[7] Universal Print job retention is the one unknown that matters.** It *sets*
Flow B's cadence. Nothing has been measured on the MFC-L5800DW; the earlier reading
came from a device since retired. To measure, poll a printed job daily on the
**printer** route (job ids are per-printer; the printer id survives a re-share):

```
GET /print/printers/{printerId}/jobs/{jobId}
```

The **last day it returns 200** is the window. Two cautions: a 404 is only evidence
when the job was created on *this* printer through the *current* share; and no such
job can exist until the PDF → PWG-raster conversion ships, since this device
accepts `image/pwg-raster` only.

---

## The three that will actually bite

| # | Timer | Why |
|---|---|---|
| 1 | UP job retention **[7]** | Unmeasured, and it is the input to Flow B's cadence. Everything else here is a knob; this is a fact nobody has yet. |
| 2 | Refresh token 90 days **[5]** | Silent until it is not. Recovery needs a human, and no alert exists. |
| 3 | The 90 s / 120 s pair **[1]** | The only chain where a violation is invisible — no response, so no retry and no notification. |

---

## Built, 2026-09-01

This section used to list two timers as "design discussion only":

| Timer | Proposed then | Shipped as |
|---|---|---|
| `PRINT_STALL_MINUTES` | 2 min | **5 min**, and it doubles as the base of the exponential schedule |
| give-up threshold | 20 min | **`PRINT_GIVE_UP_DAYS`, 10 days** — the original 20 minutes would have failed documents a printer could still have recovered |

The note ended: *"If built, these add a third and fourth clock to §5 and move
recovery off Flow C's daily pass onto Flow B's 10-minute one."* That is exactly
what happened. **Flow C and the Resubmit endpoint no longer exist**; §5 above is
the result.

One thing the original sketch got wrong: a single give-up threshold is not enough.
It conflates two limits that want different values — how much *work* an
undeliverable document may consume (`maxRetries`) and how long to keep *waiting*
(`giveUpDays`). Separating them is what creates the grace period.

Nothing else is currently proposed-but-absent.

---

Related: [design.md](design.md) §3 (ownership), §5.4 (write matrix), §6 (config),
§13 (observability) · [ai/open-defects.md](ai/open-defects.md) (G1, UC-9, S2, S4,
F2) · [deploy-to-azure.md](deploy-to-azure.md) (app settings, flow bodies).
