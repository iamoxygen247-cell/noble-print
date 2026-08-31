"""
bootstrap_token.py — sign in once as the print service account and store its
refresh token.

WHY THIS EXISTS. Universal Print's job APIs refuse app-only tokens: creating,
starting and cancelling a print job are all documented "Application: Not
supported", and createUploadSession on a printer share is "supported with
delegated permissions only". A Function App has no signed-in user, so the only
way to hold a delegated token unattended is to acquire one interactively ONCE
and keep redeeming its refresh token. This script is that one time.

Run it:
  * when first setting up an environment
  * whenever the token stops working -- a password change, a self-service
    password reset, an admin reset, or an explicit revocation will do it.
    Password EXPIRY alone will not. The endpoints say so in their 500 body.

    .\\.venv\\Scripts\\python.exe scripts\\bootstrap_token.py

Sign in as the PRINT SERVICE ACCOUNT, not as yourself: every print job will be
attributed to whoever signs in here, and the refresh token inherits that
identity's lifetime and access.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

# Corporate TLS inspection makes OpenSSL-based clients fail certificate
# validation while .NET tooling works, because .NET trusts the Windows store.
# truststore routes verification through that store. No-op where it is not
# installed, e.g. in the cloud (playbook §5.2).
os.environ.pop("REQUESTS_CA_BUNDLE", None)
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - dev-only convenience
    pass

_REPO = pathlib.Path(__file__).resolve().parent.parent
_APP = _REPO / "functionapp"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

import graph_auth  # noqa: E402  -- after the sys.path bootstrap


def _load_local_settings() -> None:
    """Read functionapp/local.settings.json so the script needs no separate
    configuration. Encoding is utf-8-SIG because Windows tooling writes this file
    with a BOM and json.loads rejects it (playbook §6.1)."""
    import json

    path = _APP / "local.settings.json"
    if not path.exists():
        return
    try:
        values = json.loads(path.read_text(encoding="utf-8-sig")).get("Values", {})
    except Exception as exc:
        print("could not read {}: {}".format(path, exc))
        return
    for key, value in values.items():
        if value and not os.getenv(key):
            os.environ[key] = str(value)


def _refresh_token_from(app, result: dict) -> str:
    """Extract the refresh token.

    MSAL usually keeps it in the token cache rather than returning it, so the
    cache is the reliable source; the direct key is checked first because some
    versions do return it.
    """
    if result.get("refresh_token"):
        return result["refresh_token"]

    from msal.token_cache import TokenCache

    entries = app.token_cache.find(TokenCache.CredentialType.REFRESH_TOKEN)
    if not entries:
        raise SystemExit(
            "sign-in succeeded but no refresh token came back. The app "
            "registration must request offline_access and allow public client "
            "flows; check both in Entra and try again."
        )
    return entries[0]["secret"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-only", action="store_true",
                        help="show the token instead of writing it to Key Vault "
                             "(for when you do not have vault access)")
    parser.add_argument("--vault", help="override KEY_VAULT_URI")
    parser.add_argument("--secret", help="override PRINT_REFRESH_TOKEN_SECRET")
    args = parser.parse_args()

    _load_local_settings()
    if args.vault:
        os.environ["KEY_VAULT_URI"] = args.vault
    if args.secret:
        os.environ["PRINT_REFRESH_TOKEN_SECRET"] = args.secret

    # A stale local override would be redeemed instead of the fresh sign-in.
    os.environ.pop("PRINT_REFRESH_TOKEN", None)

    import msal

    app = msal.PublicClientApplication(
        client_id=graph_auth.client_id(), authority=graph_auth.authority())

    flow = app.initiate_device_flow(scopes=graph_auth.SCOPES)
    if "user_code" not in flow:
        raise SystemExit(
            "could not start the device-code flow: {}\n"
            "The app registration most likely does not have public client flows "
            "enabled (Entra > App registrations > Authentication > Allow public "
            "client flows).".format(flow.get("error_description", flow))
        )

    print()
    print("=" * 72)
    print(flow["message"])
    print("Sign in as the PRINT SERVICE ACCOUNT -- print jobs are attributed to it.")
    print("=" * 72)
    print()

    result = app.acquire_token_by_device_flow(flow)  # blocks until you finish

    if "access_token" not in result:
        raise SystemExit("sign-in failed: {} {}".format(
            result.get("error"), result.get("error_description", "")))

    refresh_token = _refresh_token_from(app, result)
    account = (result.get("id_token_claims") or {}).get("preferred_username", "?")
    print("signed in as {}".format(account))

    if args.print_only:
        print()
        print("refresh token (store it yourself; treat it as a password):")
        print(refresh_token)
        return 0

    graph_auth.write_refresh_token(refresh_token)
    print("stored refresh token in secret {!r} at {}".format(
        graph_auth.secret_name(), graph_auth.key_vault_uri()))
    print()
    print("Verify with:")
    print("  .\\.venv\\Scripts\\python.exe scripts\\test.py dryrun "
          "--library \"Documents\" --folder \"/Invoices/ToPrint\" "
          "--printer-share-id <guid>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
