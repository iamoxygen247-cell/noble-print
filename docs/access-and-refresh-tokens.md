# Access tokens and refresh tokens

How the Function App gets permission to call Microsoft Graph: what each token
does, how a refresh token becomes an access token, where both live, and what
ends them. Checked on 2026-10-04 against the code, the installed MSAL (1.38.0),
Microsoft Learn, and the Entra sign-in logs, Key Vault metadata and App Insights
traces of the 2026-10-03 outage. Facts about that outage are marked
*observed*, *documented* (Microsoft Learn) or *inferred*.

---

## 1. Why it works this way

Creating, starting and cancelling a print job, and `createUploadSession` on a
printer share (the route this app uses), are **delegated-only** in Graph: there
is no application permission for them. The Function App has no signed-in user,
so its managed identity cannot make those calls. Instead:

1. A person signs in interactively with
   [`scripts/bootstrap_token.py`](../scripts/bootstrap_token.py): once at setup,
   and again whenever the refresh token stops working (§7).
2. The **refresh token** from that sign-in is stored in Key Vault.
3. The app trades it for **access tokens** when it needs one, and stores the new
   refresh token Entra returns each time.

The managed identity is still used, but only to read and write that one secret.

## 2. What each token does

### 2.1 The access token: permission for each Graph call

Every authenticated Graph request carries it as
`Authorization: Bearer <access token>`. From it, Graph learns **which app** is
calling (*Noble Universal Print*), **for which account** (the one that signed in
at the bootstrap) and **what it may do**. That last part is the delegated
scopes consented to the app, and never more than the account itself may do.

In this project it authorizes these calls:

| Service | Calls | Made by |
|---|---|---|
| SharePoint, through Graph | find the site and the library; query the queue by `Print_Status`; read a file's `driveItem` for its download URL; `PATCH` the five columns | Submit, Poll |
| Universal Print | read the printer share | Health, Submit, and Poll (to find the printer id for a cancel) |
| | create a job, `createUploadSession`, start the job | Submit |
| | read a job's status, cancel a job | Poll |

- **Short-lived.** Entra sets each token's lifetime (`expires_in`). On
  2026-10-04 it was 3756 s, about 63 minutes.
- **Memory only.** The token lives in the worker process
  (`graph_auth._cached_token`) and is shared by every call that process makes.
  This code never writes it to disk, Key Vault or a log.
- **Opaque to the app.** The code never decodes it and reads only `expires_in`,
  to know when to replace it.
- **Not sent on every call.** The SharePoint download URL and the Universal
  Print upload `PUT` carry their own credentials and must not carry it (§5.7).

### 2.2 The refresh token: permission to get the next access token

The refresh token is what lets the app obtain new access tokens **with no person
present**. Without it, someone would have to sign in about every hour.

- **Sent to one place only:** Entra's token endpoint
  (`login.microsoftonline.com`). It never goes to Graph, SharePoint or Universal
  Print.
- **Stored in one place only:** Key Vault `kv-noble-print`, secret
  `up-print-refresh-token`. It is in memory only while it is being redeemed.
- **It continues the bootstrap sign-in:** the same account, the same session,
  and the same time of the account's last MFA (§5.5). Microsoft: refresh tokens
  "are bound to a combination of user and client, but aren't tied to a
  resource".
- **Entra checks it on every use.** Revocations, password resets, consent, MFA
  age and Conditional Access all take effect at redemption (§7).
- **Replaced on every use.** Each redemption returns a new one, and the app
  stores it (§5.4).
- **Opaque.** Microsoft: "Refresh tokens are encrypted and only the Microsoft
  identity platform can read them."
- **Everything depends on it.** Every endpoint's first Graph call needs an
  access token. If the refresh token is dead, Health, Submit and Poll all fail
  before touching SharePoint or the printer.

### 2.3 At a glance

| | Access token | Refresh token | Managed identity |
|---|---|---|---|
| Used for | each Graph call | getting the next access token | reading and writing the Key Vault secret |
| Sent to | Graph | Entra's token endpoint | Key Vault |
| Kept in | worker-process memory | Key Vault | handled by `azure-identity` |
| Lasts | `expires_in` (3756 s observed) | until something in §7 ends it | handled by `azure-identity` |
| Issued by | Entra, on each redemption | Entra: at the bootstrap, then on every redemption | Azure, for `func-noble-print` |
| Acts as | the account that signed in | the account that signed in | the Function App |

**The app registration has no client secret, by design.** *Noble Universal
Print* (`bb707828-6e0e-4c2a-9f4d-329aa10c66a5`) is a **public client**: both
token paths use `msal.PublicClientApplication`, which sends only the client id.
Microsoft says a client secret "shouldn't be used in a native app". So
*Certificates & secrets* showing zero certificates, secrets and federated
credentials is correct. The app never proves its own identity to Entra; the
refresh token is the only credential on the Graph sign-in path. The storage
connection strings and the function key are separate secrets, unrelated to
Graph. `functionapp/local.settings.json` can also hold a local-development
refresh token in `PRINT_REFRESH_TOKEN`; Azure never uses it.

## 3. The flow

```
 AT SETUP, AND AFTER EACH EXPIRY        EVERY GRAPH CALL (the app)
 (a person)                             ──────────────────────────
 ───────────────────────────────        GraphClient.request()
 bootstrap_token.py                       calls get_access_token() on every HTTP attempt
   device-code sign-in + MFA                       │
         │                                cached, > 5 min left? ── yes ──► use it
         ▼                                         │ no
 Entra issues a refresh token                      ▼
         │           ┌─────────────────────┐  1. read the latest version
         └─────────► │      Key Vault      │ ──────────────────────────►
         new version │ up-print-refresh-   │  2. redeem at Entra → new access + refresh token
                     │ token               │ ◄─ 3. write the new refresh token (new version)
                     └─────────────────────┘  4. cache the access token in memory
```

## 4. The bootstrap

What [`bootstrap_token.py`](../scripts/bootstrap_token.py) does, in order:

1. Copies the non-empty `Values` from `functionapp/local.settings.json` into the
   environment, **skipping keys that are already set**.
2. Applies `--vault` / `--secret`, then removes `PRINT_REFRESH_TOKEN`, a
   local-development override that the local settings file may set and that has
   no part in a bootstrap.
3. Starts an MSAL **device-code** flow for `graph_auth.SCOPES` and prints a code
   to enter at the device-login page.
4. Writes the refresh token (from the result, or from MSAL's cache) to Key Vault
   with `graph_auth.store_refresh_token`, and prints the new version. If the
   write fails, the script stops with an error and exit code 1, saying that
   nothing was written. Before 2026-10-04 it printed success either way. The
   sign-in also returns an access token, which the script discards. The app's
   first access token comes from its own first redemption (§5).

### 4.1 Running it

```powershell
cd "C:\Users\georg\dev\Noble Homes\Invoice Extractor\noble-print"

# Only behind TLS inspection (the dev PC, 2026-10-04). Both lines last for this window only.
$env:PYTHONPATH = "$env:LOCALAPPDATA\az-truststore"             # the venv has no truststore
$env:PATH = "$env:LOCALAPPDATA\az-truststore\shim;$env:PATH"    # for the az.cmd the vault write starts

.\.venv\Scripts\python.exe scripts\bootstrap_token.py --vault https://kv-noble-print.vault.azure.net/
```

Then:

1. Open the device-login page in a **private browser window**, so MFA is
   actually prompted (§7). Continue only if the page names *Noble Universal
   Print* and the code matches the one the script printed. Sign in as the
   intended account (§4.2) and complete MFA.
2. Read the output. It should say `signed in as <account>`, then
   `stored refresh token in secret ... (version <id>, created <time>)`. If it
   stops with `could not store the refresh token ...` instead, nothing was
   written: fix the cause it names and run it again.
3. Cross-check the write. The Versions blade's *CURRENT VERSION* should be the
   version the script printed, or:

   ```powershell
   # prints UTC (+00:00): Vancouver + 7 h in summer, + 8 h in winter
   az keyvault secret show --vault-name kv-noble-print --name up-print-refresh-token --query "attributes.created" -o tsv
   ```

4. Ignore the script's closing `Verify with: scripts\test.py dryrun ...` hint.
   `test.py` defaults to the local host, which uses `local.settings.json`'s own
   token, not Key Vault.

> **Why each line matters.**
>
> - **`--vault`.** Step 1 loads `functionapp/local.settings.json`. A copy made
>   from the template still holds `KEY_VAULT_URI =
>   https://YOUR-KEYVAULT.vault.azure.net/`, and this machine's did on
>   2026-10-04. The runbook's `$env:KEY_VAULT_URI = "https://$VAULT.vault.azure.net/"`
>   becomes `https://.vault.azure.net/` in a new window where `$VAULT` is not
>   set. That value is non-empty, so it also blocks the settings file. Either
>   way, the write fails only after you have signed in.
> - **The write must succeed.** Until 2026-10-04 the script used the runtime's
>   best-effort write, so a failed write printed a warning and then
>   `stored refresh token ...` anyway. It now stops with an error that names the
>   vault and says nothing was written.
> - **The write uses your own Azure sign-in.** `DefaultAzureCredential` runs
>   `az.cmd` as a child process, so a PowerShell-profile `az` wrapper does not
>   apply. You need *Key Vault Secrets Officer* on `kv-noble-print`.
> - **TLS.** Measured on 2026-10-04 on the dev PC: the venv's request to
>   `login.microsoftonline.com` failed with `SSLError` without the
>   `PYTHONPATH` line, and returned 200 with it.

### 4.2 Who signs in

Every print job is attributed to the account that signs in. The token can do
whatever that account can do within the scopes below, and it is subject to that
account's MFA and password policy (§7). The runbook asks for a dedicated print
service account
([deploy-to-azure.md §1](deploy-to-azure.md#the-service-account)). The token
that failed on 2026-10-03 belonged to an administrator's own account instead
(*observed*); §8 shows how to check.

A dedicated account changes who the jobs are attributed to and what the token
can reach. It does **not** avoid the MFA expiry in §7 if that account is also
under per-user MFA, because the remember-MFA period is tenant-wide.

### 4.3 Scopes

[`graph_auth.SCOPES`](../functionapp/graph_auth.py#L71): `Sites.ReadWrite.All`,
`PrintJob.ReadWriteBasic`, `PrintJob.Create`, `Printer.Read.All`,
`PrinterShare.ReadBasic.All`. MSAL adds `offline_access`, `openid` and
`profile` itself.

Every redemption asks for all of `SCOPES`. So once deployed code asks for a
scope that has no consent, every redemption fails with `AADSTS65001`
("hasn't consented"). The fix recorded on 2026-09-01
([troubleshooting.md](ai/troubleshooting.md)) was admin consent followed by a
re-run of the bootstrap. Microsoft documents that refresh tokens "aren't tied
to a resource", so consent alone may be enough, but that was not tested.

## 5. How an access token is made from a refresh token

The app never creates a token; Entra does. The app decides **when** it needs a
new access token, **asks** Entra for one using the stored refresh token, and
**keeps** what comes back: the access token in memory and the new refresh
token in Key Vault. All of it is in
[`graph_auth.py`](../functionapp/graph_auth.py).

### 5.1 The logic, step by step

[`function_app._client()`](../functionapp/function_app.py#L172) builds
`GraphClient(graph_auth.get_access_token)`, and
[`GraphClient.request`](../functionapp/graph_client.py#L165) calls
`get_access_token()` before **every HTTP attempt**.

1. **Reuse when possible.** If this worker process holds an access token with
   more than 5 minutes (`REFRESH_MARGIN_SECONDS`) left, return it. This takes no
   lock and makes no call to Key Vault or Entra. Almost every call ends here.
2. **Otherwise, one thread at a time.** Take a process-wide lock, so that
   threads needing a token at the same moment cause one redemption, not one
   each. Check the cache again once the lock is held: a thread that waited will
   find the token the first thread just obtained, and return it.
3. **Read the refresh token.** This is the latest version of the Key Vault
   secret, or `PRINT_REFRESH_TOKEN` in local development. If Key Vault cannot be
   read, raise `AuthBootstrapRequired`.
4. **Redeem it at Entra** with `_redeem` (§5.3). Entra answers with a new access
   token, its lifetime and a new refresh token. If it answers with an error,
   `_redeem` raises it.
5. **Store the new refresh token** as a new Key Vault version, if the response
   includes one. This is best-effort: a failure is logged as a warning and the
   call carries on (§6).
6. **Cache the access token** until `now + expires_in`, log
   `acquired a Graph access token, valid for …s`, and return it.

The cache is a module global, so each worker process has its own. A cold start
always redeems, and a warm process redeems at most once per access-token
lifetime. In production that has meant one or two redemptions, and so one or
two new Key Vault versions, per flow run. The flow ran hourly in early September
and every 3 hours by October.

### 5.2 The code: `get_access_token()`

From [`graph_auth.py`](../functionapp/graph_auth.py#L215). The docstring is
omitted and the `# step` markers are added:

```python
def get_access_token() -> str:
    global _cached_token, _cached_expiry

    now = time.time()
    if _cached_token and now < _cached_expiry - REFRESH_MARGIN_SECONDS:   # step 1
        return _cached_token

    with _lock:                                                           # step 2
        # Re-check: another thread may have refreshed while we waited.
        now = time.time()
        if _cached_token and now < _cached_expiry - REFRESH_MARGIN_SECONDS:
            return _cached_token

        result = _redeem(read_refresh_token())                            # steps 3-4

        rotated = result.get("refresh_token")                             # step 5
        if rotated:
            write_refresh_token(rotated)

        _cached_token = result["access_token"]                            # step 6
        _cached_expiry = time.time() + int(result.get("expires_in", 3600))
        logging.info("acquired a Graph access token, valid for %ss",
                     result.get("expires_in", "?"))
        return _cached_token
```

### 5.3 The exchange with Entra: `_redeem()`

From [`graph_auth.py`](../functionapp/graph_auth.py#L195), with comments omitted:

```python
def _redeem(refresh_token: str) -> dict:
    import msal

    app = msal.PublicClientApplication(client_id=client_id(), authority=authority())
    result = app.acquire_token_by_refresh_token(refresh_token, scopes=SCOPES)

    if "access_token" not in result:
        error = result.get("error", "unknown_error")
        description = result.get("error_description", "")
        if error in ("invalid_grant", "interaction_required", "invalid_client"):
            raise AuthBootstrapRequired(
                f"the stored refresh token is no longer valid ({error}). "
                f"Re-run scripts/bootstrap_token.py to sign in again. {description}"[:500]
            )
        raise RuntimeError(f"token refresh failed ({error}): {description}"[:500])
    return result
```

- **`PublicClientApplication`** identifies the app by client id alone, with no
  secret. `authority` is `https://login.microsoftonline.com/<GRAPH_TENANT_ID>`.
  A new MSAL object is built for every redemption, so MSAL's own token cache
  plays no part: the app keeps its tokens itself.
- **`acquire_token_by_refresh_token`** adds `openid`, `profile` and
  `offline_access` to the five scopes, then POSTs a refresh grant to the
  tenant's token endpoint. Simplified, since MSAL also sends a few housekeeping
  fields:

  ```http
  POST https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token
  Content-Type: application/x-www-form-urlencoded

  client_id=bb707828-…
  &grant_type=refresh_token
  &refresh_token=<the token read from Key Vault>
  &scope=<the five SCOPES> openid profile offline_access
  ```

- **On success** Entra returns the documented shape (abridged).
  `get_access_token` uses `access_token`, `expires_in` and `refresh_token`, and
  ignores the rest:

  ```json
  {
    "token_type": "Bearer",
    "scope": "<granted scopes>",
    "expires_in": 3756,
    "access_token": "<new access token>",
    "refresh_token": "<new refresh token>",
    "id_token": "<id token>"
  }
  ```

- **On failure** Entra returns `error` and an `error_description` that starts
  with the `AADSTS` code. Here is the 2026-10-03 failure in that format
  (abridged):

  ```json
  {
    "error": "invalid_grant",
    "error_description": "AADSTS50078: Presented multi-factor authentication has expired due to policies configured by your administrator, … Trace ID: … Correlation ID: … Timestamp: …",
    "error_codes": [50078]
  }
  ```

  The code 50078 is *observed* in the sign-in log. `invalid_grant` is
  *inferred*: the flow showed `(invalid_g…`, and the code maps only three
  values, of which only `invalid_grant` starts that way. Microsoft does not
  document which `error` comes with 50078. `invalid_grant`,
  `interaction_required` and `invalid_client` become `AuthBootstrapRequired`
  ("someone must sign in again"), carrying the first 500 characters of the
  message. Any other error becomes a `RuntimeError`.

### 5.4 Is a new refresh token created every time an access token is?

**Yes: they arrive as a pair.** The app gets an access token only by redeeming a
refresh token, and Entra answers every redemption with a new refresh token as
well. In Microsoft's words, refresh tokens "replace themselves with a fresh
token upon every use". The app stores each one, so every redemption adds a Key
Vault version: 667 of them between 2026-09-04 and 2026-10-04.

Three qualifications:

- **Not on every Graph call.** Graph calls reuse the cached access token
  (§5.1, step 1). Tokens are created only by a redemption, which in production
  happens once or twice per flow run.
- **Not for the bootstrap's access token.** The device-code sign-in also returns
  an access token. The script discards it and stores only the refresh token.
- **Only because `offline_access` is requested.** Microsoft documents the new
  refresh token as "only provided if `offline_access` scope was requested", and
  MSAL adds that scope to every request. If a response ever arrived without one,
  the code would write nothing and keep using the stored token
  (`test_a_response_without_a_new_refresh_token_is_not_a_failure`).

### 5.5 How the next refresh token is created

**Entra creates it**, in the same response as the access token; the app never
builds or changes a token. The app's part is to store it
(`write_refresh_token` → `store_refresh_token` → `set_secret`, which creates a
new version) and to read
the newest version next time (`read_refresh_token` → `get_secret` with no
version, which returns the latest).

The new token continues the existing sign-in; it does not start a new one:

- **Same account.** Microsoft: refresh tokens "are valid for all permissions
  that your client has already received consent for".
- **Same session, and so the same last MFA.** *Documented:* "When a refresh
  token is validated, Microsoft Entra ID checks that the last multifactor
  authentication occurred within the specified number of days." A redemption is
  non-interactive, so it adds no MFA; only an interactive sign-in does.
  *Observed:* the sign-in logs show one session id on every redemption, from the
  oldest retained entry (2026-09-06) to the failures that began on 2026-10-03.
  Using the token every few hours did not stop the MFA from ageing (§7).
- **Fresh, so regular use stops it expiring from inactivity.** The default
  lifetime is 90 days, and each use replaces the token with a fresh one. That
  does not get past the MFA check or anything else in §7.
- **The old one is not revoked.** Microsoft: the platform "doesn't revoke old
  refresh tokens when used to fetch new access tokens", and it advises to
  "securely delete the old refresh token after acquiring a new one". This app
  deletes nothing: every earlier version stays enabled in the vault (§6).

### 5.6 What it looks like in the logs

A healthy cold start in App Insights (2026-10-04, UTC):

```
01:00:06.15  GET kv-noble-print/secrets/up-print-refresh-token  401, then 200   ← auth challenge, then read
01:00:06.80  PUT kv-noble-print/secrets/up-print-refresh-token  200             ← new refresh token stored
01:00:07.21  acquired a Graph access token, valid for 3756s
01:00:07.80  RUN_SUMMARY ep=health ... ok=1 failed=0
```

The 401 is the Key Vault SDK's normal challenge before it attaches the
managed-identity token. The call to Entra is not logged, only its outcome. A
failed redemption shows the same GET, then no PUT, no `acquired` line, and
`RUN_SUMMARY ep=health ... failed=1`.

### 5.7 Calls that must not carry the token, and a rejected token

**Calls that must not carry it.** The SharePoint pre-authenticated download URL
and the Universal Print upload-session `PUT` carry their own credentials. They
go through `graph_client`'s separate unauthenticated session
(`download_unauthenticated`, `put_unauthenticated`, `delete_unauthenticated`).
Graph documents that a bearer token on the upload `PUT` "might result in an HTTP
401". `tests/test_adapters.py` asserts the header is absent.

**A rejected access token.** A Graph `401` raises at once and is not retried:
`RETRYABLE_STATUS` covers only 429, 503 and 504. Production code never calls
`graph_auth.reset_cache()`, so a worker keeps its cached token until the
5-minute margin.

## 6. Key Vault versions

Each successful redemption writes its new refresh token as a **new version** of
the secret, and each bootstrap writes one too. There were 667 versions between
2026-09-04 and 2026-10-04.

- **Best-effort.** A failed write logs a warning and the request carries on. The
  access token is already valid, and the old refresh token was not revoked, so
  the next run simply redeems the old one.
- **Safe to race.** Two workers that redeem at once both succeed, and the last
  write wins.
- **Needs *Key Vault Secrets Officer*.** With *Secrets User* (read-only) every
  write fails quietly and the stored token never advances
  ([deploy-to-azure.md §4.2](deploy-to-azure.md#42-grant-the-app-access-to-the-vault)).
- **The newest version's `created` time (UTC) is the last successful
  write-back**, from a redemption or a bootstrap. If writes fail, it stops
  moving while redemptions still succeed.
- **Older versions are kept, and are not a fallback.** None carries a newer MFA
  than the current one, so whatever ended the newest ended them too.

## 7. What ends a refresh token

Using the token regularly does **not** keep it alive indefinitely. Whatever the
cause, the flow sees the same result: Health returns `200` with
`healthy: false` and `AUTH_BOOTSTRAP_REQUIRED`, and Submit and Poll return `500`
with `remedy: run scripts/bootstrap_token.py`. **Only the message text tells
the causes apart** (§8).

| Cause | Message | Seen here |
|---|---|---|
| **The last MFA is older than the tenant allows** | `AADSTS50078`: "Presented multi-factor authentication has expired due to policies configured by your administrator…" | **2026-10-03 21:00 local**, and every run since |
| A scope is requested without consent | `AADSTS65001` | 2026-09-01 |
| Password changed or reset (if the sign-in used a password), or the user's refresh tokens revoked | typically `AADSTS50173` | — |
| Not redeemed for 90 days, for example with the flows switched off | `AADSTS700082` | — |
| Conditional Access: sign-in frequency, or a policy that blocks device-code flow | `AADSTS70043`, `AADSTS530036` | — |
| Key Vault unreadable | `could not read the refresh token …` | — |

The last two Conditional Access rows matter for the future. Microsoft
"recommends blocking device code flow wherever possible", and a device-code
session stays "protocol tracked" through every refresh. A policy that blocks
device-code flow would stop both the bootstrap and the running app.

**The MFA row in detail.**

- *Documented.* "When a refresh token is validated, Microsoft Entra ID checks
  that the last multifactor authentication occurred within the specified number
  of days." That number is the *remember multifactor authentication* setting
  (1–365 days), a **tenant-wide** per-user-MFA service setting. Rotation does
  not reset it: only an interactive sign-in with MFA does.
- *Observed (2026-10-03).* The requirement came from *Per-user MFA* and the
  session policy was *Remember MFA*. The secret's first version was written at
  2026-09-04 03:18:05 UTC. The last success came 29 d 21 h 42 m later, and the
  first failure 30 d 0 h 42 m later. The Entra audit log has no change to the
  account or to any policy between 17:30 and 21:30 local.
- *Inferred.* The token's MFA dates from the bootstrap evening, and the setting
  is 30 days, the only whole number of days that fits.

**Confirm it by reading the setting.** Go to *Entra ID → Users → Per-user MFA →
service settings → remember multifactor authentication*, or to *Entra ID →
Multifactor authentication → Configure → Additional cloud-based MFA settings*.

| The setting reads | Meaning |
|---|---|
| 30 days | The inference holds. Expect the next failure 30 days after the MFA completed at the next bootstrap. |
| More than 30 (Microsoft training material gives 90 as the default) | The token's MFA predates the bootstrap, from an earlier sign-in or a browser that remembered MFA. Microsoft does not document that case, and the interval cannot be predicted from the bootstrap date. |
| Less than 30 | It was lowered after the 18:00 success; a chain could not have lasted 30 days under it. |

## 8. Diagnosing

1. **The error text.** In Power Automate, open *Parse Health* (or the Health
   action's raw outputs) and read `errors[0].message`. An illustrative example:

   ```
   the stored refresh token is no longer valid (invalid_grant). Re-run scripts/bootstrap_token.py to sign in again. AADSTS50078: Presented multi-factor ...
   ```

   The message is cut at 500 characters. The `AADSTS` code follows a prefix of
   113 characters for `invalid_grant` (114 for `invalid_client`, 120 for
   `interaction_required`), so the cut never hides it. Health does **not** log
   this text, only `RUN_SUMMARY ... failed=1`. Submit and Poll log it as
   `delegated auth is broken: ...`, but only if they are called.

2. **Entra sign-in logs.** Go to *Entra ID → Monitoring & health → Sign-in logs*,
   choose the non-interactive user sign-ins, and filter on the application
   *Noble Universal Print*. A failed row shows the error code, its failure
   reason, and the **account that owns the token**.

   Through Graph, the policy behind a failure (`authenticationRequirementPolicies`,
   `sessionLifetimePolicies`) is on the **beta** `signIn` resource only. Beta
   returns non-interactive rows only with
   `$filter=signInEventTypes/any(t: t eq 'nonInteractiveUser')`, and the API
   needs Entra ID P1 or P2. Sign-ins are kept for 30 days (7 on Free), roughly
   one MFA interval. So by the time this fails, the bootstrap sign-in has
   normally aged out.

3. **Key Vault timeline.** The newest version is the last successful write-back
   (§6):

   ```powershell
   az keyvault secret show --vault-name kv-noble-print --name up-print-refresh-token --query "attributes.created" -o tsv
   ```

4. **App Insights.** Redemptions and Health results side by side:

   ```kusto
   traces
   | where timestamp > ago(3d)
   | where message startswith "acquired a Graph access token"
        or message startswith "RUN_SUMMARY ep=health"
   | project timestamp, message
   | order by timestamp asc
   ```

## 9. Recovery

1. Read the message (§8). If it is `AADSTS65001`, grant consent first. If it is
   "could not read the refresh token", fix Key Vault access; the token may be
   fine.
2. Run the bootstrap as in §4.1: use a private browser window, sign in as the
   intended account, and complete MFA.
3. Confirm the new Key Vault version (§4.1). Also confirm that the sign-in logs
   show an interactive sign-in to *Noble Universal Print* in which MFA was
   performed, rather than "Previously satisfied".
4. Re-run the flow, or wait for the next scheduled run. Health should return
   `healthy: true`, and one more version appears when that run rotates the
   token. That proves the managed identity can still write.
5. Nothing else is needed: no restart, no redeploy (§5.1), and no SharePoint
   cleanup. Health writes nothing to SharePoint or Universal Print, and Flow A
   stops before Submit when Health is unhealthy. Files queued meanwhile wait at
   `PRINT_READY`, and Submit takes up to `batchSize` of them per call.
6. If the cause was `AADSTS50078`, set a reminder a few days before the MFA in
   step 3 reaches the tenant's remember-MFA period (§7).

## 10. Code map

| Concern | Where | Pinned by (`tests/test_auth.py` unless named) |
|---|---|---|
| Scopes | `graph_auth.SCOPES` | `test_the_scopes_exclude_the_reserved_ones`; `test_the_scopes_cover_every_operation_the_app_performs` (checks four of the five, not `PrinterShare.ReadBasic.All`) |
| Secret name, 5-minute margin | `DEFAULT_SECRET_NAME`, `REFRESH_MARGIN_SECONDS` | none |
| Access-token cache | `graph_auth.get_access_token` | `test_a_live_token_is_reused_without_touching_key_vault`, `test_a_token_near_expiry_is_refreshed_exactly_once` |
| Rotation | `get_access_token` → `write_refresh_token` | `test_the_rotated_refresh_token_is_written_back`, `test_each_refresh_redeems_the_most_recently_stored_token`, `test_a_response_without_a_new_refresh_token_is_not_a_failure`, `test_a_key_vault_write_failure_does_not_fail_the_request`. Only the last runs the real `write_refresh_token`. |
| Reading the refresh token, `PRINT_REFRESH_TOKEN` | `graph_auth.read_refresh_token` | none: the tests replace it |
| Redeem and map errors | `graph_auth._redeem` | `test_a_revoked_token_asks_for_the_bootstrap_script`, `test_an_unexpected_token_error_is_not_mistaken_for_a_dead_token` |
| Bearer header, token fetched per attempt | `graph_client.GraphClient.request` | none |
| No token on download or upload | `graph_client.*_unauthenticated` | `tests/test_adapters.py`: `test_download_url_is_fetched_without_an_authorization_header`, `test_the_upload_put_carries_no_authorization_header` |
| Error to flow response | `function_app.check_printer_health`, `function_app._server_error` | `test_the_endpoint_surfaces_the_remedy`, `tests/test_health.py` |
| The bootstrap's write, which must fail loudly | `scripts/bootstrap_token.py` → `graph_auth.store_refresh_token` | `test_the_bootstrap_stops_when_the_vault_write_fails`, `test_the_bootstrap_reports_the_version_it_stored`, `test_the_bootstrap_write_raises_where_the_rotation_write_swallows`. The sign-in itself is faked. |
