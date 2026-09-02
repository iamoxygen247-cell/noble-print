"""
graph_auth.py — delegated Graph access tokens for an unattended service.

WHY THIS IS NOT MANAGED IDENTITY. Universal Print's job APIs do not accept
app-only tokens. The documentation is explicit -- "Application: Not supported" --
for creating a job (both the printerShare and printer routes), starting one, and
cancelling one, and createUploadSession on a share is "supported with delegated
permissions only". A Function App has no signed-in user, so the only way to hold
a delegated token unattended is to acquire one interactively ONCE and keep
redeeming its refresh token.

The shape, therefore:

    scripts/bootstrap_token.py   device-code sign-in as the print service
                                 account, run by a human, once
              |
              v
    Key Vault secret             the refresh token, at rest
              |
              v
    this module                  redeem -> access token (cached in-process),
                                 write the rotated refresh token back

ROTATION IS SAFE TO RACE. Entra returns a new refresh token on every redemption
and "doesn't revoke old refresh tokens when used to fetch new access tokens", so
two concurrent invocations both succeed and last-write-wins stores a valid token.
An earlier draft of this design serialised the read-redeem-write with a blob
lease; it was solving a problem that does not exist and was removed.

WHAT BREAKS IT. The refresh token lasts 90 days and is revoked by a password
change, a self-service password reset, an admin password reset, or an explicit
revocation. Password *expiry* alone does not revoke it. When it does break, every
endpoint fails the same way and the fix is always the same, so the error names the
script to run rather than making someone diagnose it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

# Delegated scopes. offline_access / openid / profile are added by MSAL itself
# and must not be listed here -- MSAL rejects the reserved scopes.
#
# THE TWO PRINTJOB SCOPES ARE BOTH REQUIRED AND NEITHER IS REDUNDANT. Submitting
# a document is four calls and no single "Basic" scope covers all four. Verified
# against the v1.0 permission tables on 2026-09-01:
#
#   call                    least privileged        ReadWriteBasic accepted?
#   POST .../jobs           PrintJob.ReadWriteBasic  yes
#   createUploadSession     PrintJob.Create          NO -- Create or ReadWrite only
#   POST .../start          PrintJob.Create          yes
#   POST .../cancel         PrintJob.ReadWriteBasic  yes
#
# So createUploadSession is the odd one out, and with ReadWriteBasic alone the
# pipeline gets a job created, a document created, and then a 403 "The token does
# not have one or more required security scopes" -- after the row is claimed
# (defect L3). PrintJob.ReadWrite would also cover all four in one scope; two
# narrow scopes are preferred to one broad one.
#
# ADDING A SCOPE HERE IS NOT ENOUGH. The refresh token carries the scopes that
# were consented when it was minted, so a new scope needs the delegated
# permission added in Entra AND a fresh scripts/bootstrap_token.py sign-in.
SCOPES = [
    "https://graph.microsoft.com/Sites.ReadWrite.All",
    "https://graph.microsoft.com/PrintJob.ReadWriteBasic",
    "https://graph.microsoft.com/PrintJob.Create",
    "https://graph.microsoft.com/Printer.Read.All",
    "https://graph.microsoft.com/PrinterShare.ReadBasic.All",
]

DEFAULT_SECRET_NAME = "up-print-refresh-token"
# Refresh this far before expiry. An access token is good for ~60-90 minutes; the
# margin covers a long invocation that started just under the wire.
REFRESH_MARGIN_SECONDS = 300


class AuthBootstrapRequired(RuntimeError):
    """The refresh token is gone, expired, or revoked. Recovering needs a human
    at an interactive sign-in -- there is no automatic path back."""


def _required(name: str) -> str:
    """Config that names an environment gets no default: a silent fallback could
    point the app at the wrong tenant or the wrong vault (playbook §2.7)."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. It identifies this environment and has no safe "
            f"default; set it in Application Settings (or local.settings.json)."
        )
    return value


def tenant_id() -> str:
    return _required("GRAPH_TENANT_ID")


def client_id() -> str:
    return _required("GRAPH_CLIENT_ID")


def key_vault_uri() -> str:
    return _required("KEY_VAULT_URI")


def secret_name() -> str:
    return os.getenv("PRINT_REFRESH_TOKEN_SECRET") or DEFAULT_SECRET_NAME


def authority() -> str:
    return f"https://login.microsoftonline.com/{tenant_id()}"


# --- refresh-token storage ----------------------------------------------------


def _secret_client():
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    # AZURE_CLIENT_ID disambiguates when the app has several managed identities.
    return SecretClient(vault_url=key_vault_uri(), credential=DefaultAzureCredential())


def read_refresh_token() -> str:
    """The stored refresh token.

    PRINT_REFRESH_TOKEN is a LOCAL DEVELOPMENT escape hatch so `func start` works
    without Key Vault access. It logs a warning on every use precisely so that if
    it ever reaches a deployed app the evidence is in App Insights -- a local
    convenience setting silently overriding the cloud path is a failure mode the
    sibling project has already paid for once.
    """
    direct = os.getenv("PRINT_REFRESH_TOKEN")
    if direct:
        logging.warning("using PRINT_REFRESH_TOKEN from the environment; this is a "
                        "local-development escape hatch and must not be set in Azure")
        return direct
    try:
        return _secret_client().get_secret(secret_name()).value
    except Exception as exc:
        raise AuthBootstrapRequired(
            f"could not read the refresh token '{secret_name()}' from {key_vault_uri()}: "
            f"{exc}. If the secret is missing, run scripts/bootstrap_token.py."
        )


def write_refresh_token(value: str) -> None:
    """Persist the rotated refresh token. Best-effort: the access token we just
    obtained is valid regardless, and failing the request because we could not
    file the *next* token away would turn a warning into an outage. The stored
    token also stays valid (Entra does not revoke it on rotation), so the next
    invocation simply rotates from where this one started."""
    if os.getenv("PRINT_REFRESH_TOKEN"):
        return  # local dev: nothing to write back to
    try:
        _secret_client().set_secret(secret_name(), value)
    except Exception:
        logging.warning("could not write the rotated refresh token back to Key Vault; "
                        "the previous token remains valid", exc_info=True)


# --- access-token cache -------------------------------------------------------

_lock = threading.Lock()
_cached_token: Optional[str] = None
_cached_expiry: float = 0.0


def reset_cache() -> None:
    """Drop the cached access token. For tests, and for a caller that has just
    seen a 401 and wants the next attempt to re-acquire."""
    global _cached_token, _cached_expiry
    with _lock:
        _cached_token = None
        _cached_expiry = 0.0


def _redeem(refresh_token: str) -> dict:
    import msal

    app = msal.PublicClientApplication(client_id=client_id(), authority=authority())
    result = app.acquire_token_by_refresh_token(refresh_token, scopes=SCOPES)

    if "access_token" not in result:
        error = result.get("error", "unknown_error")
        description = result.get("error_description", "")
        # invalid_grant is the whole family of "the token is no longer usable":
        # expired, revoked, password changed, consent withdrawn.
        if error in ("invalid_grant", "interaction_required", "invalid_client"):
            raise AuthBootstrapRequired(
                f"the stored refresh token is no longer valid ({error}). "
                f"Re-run scripts/bootstrap_token.py to sign in again. {description}"[:500]
            )
        raise RuntimeError(f"token refresh failed ({error}): {description}"[:500])
    return result


def get_access_token() -> str:
    """A valid Graph access token. This is the `token_provider` handed to
    GraphClient, so it is called on every request and must stay cheap: the
    common path is a dictionary read, and Key Vault is touched only when the
    cached token is within REFRESH_MARGIN_SECONDS of expiry."""
    global _cached_token, _cached_expiry

    now = time.time()
    if _cached_token and now < _cached_expiry - REFRESH_MARGIN_SECONDS:
        return _cached_token

    with _lock:
        # Re-check: another thread may have refreshed while we waited.
        now = time.time()
        if _cached_token and now < _cached_expiry - REFRESH_MARGIN_SECONDS:
            return _cached_token

        result = _redeem(read_refresh_token())

        rotated = result.get("refresh_token")
        if rotated:
            write_refresh_token(rotated)

        _cached_token = result["access_token"]
        _cached_expiry = time.time() + int(result.get("expires_in", 3600))
        logging.info("acquired a Graph access token, valid for %ss",
                     result.get("expires_in", "?"))
        return _cached_token
