# Timing boundaries — every clock in the pipeline

Every timer, timeout, window and cadence this system depends on. Values read from
the **code**, not the other docs — a few of those have drifted.

### Legend — who controls each timer

| | Owner | Change it by | Redeploy? |
|---|---|---|---|
| 🔧 | **App — setting** | Function App application setting | no |
| 🛠 | **App — code** | edit the constant in source | **yes** |
| 📄 | **App — host.json** | edit `functionapp/host.json` | **yes** |
| 🔁 | **Power Automate** | edit the flow's Recurrence trigger | no |
| 🔒 | **Azure / Microsoft** | **cannot be changed** | — |

---

## At a glance

| Owner | Count | Examples |
|---|---|---|
| 🔧 App — setting | 5 | `GRAPH_TIMEOUT_SECONDS`, `PRINT_BUDGET_SECONDS`, the two windows |
| 🛠 App — code | 4 | retry attempts, backoff, `Retry-After` cap, token refresh margin |
| 📄 App — host.json | 1 | `functionTimeout` (currently unset) |
| 🔁 Power Automate | 4 | Flow A / B / C / D recurrences |
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

## 5. Business windows — measured in UTC against `createdDateTime`

| Timer | Value | Range | Owner | Defined in |
|---|---|---|---|---|
| `PRINT_STATUS_WINDOW_DAYS` | **20 days** | 1–365 | 🔧 setting + request `windowDays` | [print_policy.py:71](../functionapp/print_policy.py#L71) |
| `PRINT_RESUBMIT_MIN_AGE_HOURS` | **72 hours** | 1–8760 | 🔧 setting + request `minAgeHours` | [print_policy.py:75](../functionapp/print_policy.py#L75) |
| `PRINT_BUSINESS_TZ` | `America/Vancouver` | — | 🔧 setting | [print_policy.py:79](../functionapp/print_policy.py#L79) |

- **20 days** — trailing window. Poll and Resubmit *both* exclude rows outside it,
  so a row stuck past 20 days is never touched again (**G1, the 20-day cliff**).
  Poll reports them as `staleCount` / `staleItems` — the only automatic signal.
- **72 hours** — gates Resubmit (R15), measured from file creation (R16).
  Boundary is **inclusive**, so a file exactly 72 h old is eligible.
- **`PRINT_BUSINESS_TZ` is display only** — the `printed on …` message. Window
  arithmetic never touches it, and the two paths are separate tested functions
  because mixing them is how daylight-saving bugs get in.

> **Two clocks, easily conflated:**
> `age = now − createdDateTime` — never changes; drives both windows above.
> `idle = now − lastModifiedDateTime` — our writes bump it; orders Resubmit's
> retry queue. Using `age` for that queue was defect **F2**.

## 6. Power Automate cadences

**No timer trigger exists anywhere in the function app** — all three routes are
HTTP-triggered. Every row here is a flow edit with nothing to redeploy.

| Flow | Cadence | Owner |
|---|---|---|
| **A — Submit** | every **15 min**, `Do Until remainingReady == 0` **or 10 iterations** | 🔁 flow |
| **B — Poll** | every **10 min** | 🔁 flow |
| **C — Resubmit** | **daily, 02:00** | 🔁 flow |
| **D — Weekly digest** | **Mondays 07:00** | 🔁 flow |

Note **[6]** — Flow B's cadence is a correctness control, not a preference.

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
Poll sees it, Poll gets a 404, writes nothing, and Resubmit reprints the document
72 hours later. The mitigation is entirely the cadence — and the cadence should be
set from the measurement in **[7]**, which has not been taken.

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

## Not implemented

Confirmed absent from `functionapp/` by grep, 2026-09-01. Recorded so they are not
mistaken for settings that can be changed.

| Timer | Proposed | Status |
|---|---|---|
| `PRINT_STALL_MINUTES` | 2 min | Design discussion only — would requeue a stalled `PRINT_PENDING` row. |
| give-up threshold | 20 min | Design discussion only — would mark a long-pending row `PRINT_FAILED`. |

If built, these add a third and fourth clock to §5 and move recovery off Flow C's
daily pass onto Flow B's 10-minute one.

---

Related: [design.md](design.md) §3 (ownership), §5.4 (write matrix), §6 (config),
§13 (observability) · [ai/open-defects.md](ai/open-defects.md) (G1, UC-9, S2, S4,
F2) · [deploy-to-azure.md](deploy-to-azure.md) (app settings, flow bodies).
