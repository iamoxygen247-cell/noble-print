"""
test_auth.py — Tier C: delegated token handling.

Universal Print refuses app-only tokens, so the whole pipeline hangs off one
refresh token in Key Vault. Three behaviours matter enough to pin:

* the rotated refresh token is written back on EVERY redemption. Entra returns a
  new one each time; failing to persist it means the stored token eventually
  falls outside its 90-day life (sooner, if the MFA inside it ages out first)
  and the pipeline stops with no code change to blame.

* a dead token produces an error that names the fix. This failure is
  unrecoverable without a human at an interactive sign-in, and it looks identical
  to a dozen other 500s unless the message says so.

* the bootstrap's Key Vault write fails loudly. The runtime's rotation write is
  best-effort, but scripts/bootstrap_token.py exists only to store a token, so a
  failed write must stop it rather than print "stored".
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import function_app
import graph_auth
from helpers import SITE, SUBMIT, as_json, post

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.fixture(autouse=True)
def clean_token_cache(monkeypatch):
    graph_auth.reset_cache()
    monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-guid")
    monkeypatch.setenv("GRAPH_CLIENT_ID", "client-guid")
    monkeypatch.setenv("KEY_VAULT_URI", "https://kv.vault.azure.net/")
    yield
    graph_auth.reset_cache()


@pytest.fixture
def vault(monkeypatch):
    """An in-memory stand-in for the Key Vault secret."""
    store = {"secret": "refresh-token-v1", "writes": []}

    monkeypatch.setattr(graph_auth, "read_refresh_token", lambda: store["secret"])

    def write(value):
        store["writes"].append(value)
        store["secret"] = value

    monkeypatch.setattr(graph_auth, "write_refresh_token", write)
    return store


def redemption(monkeypatch, *, expires_in=3600, rotate=True):
    """Record redemptions and hand back a fresh token pair."""
    calls = []

    def fake_redeem(refresh_token):
        calls.append(refresh_token)
        result = {"access_token": "access-{}".format(len(calls)),
                  "expires_in": expires_in}
        if rotate:
            result["refresh_token"] = "refresh-token-v{}".format(len(calls) + 1)
        return result

    monkeypatch.setattr(graph_auth, "_redeem", fake_redeem)
    return calls


# --- caching ------------------------------------------------------------------


def test_a_live_token_is_reused_without_touching_key_vault(monkeypatch, vault):
    """get_access_token runs on every Graph request, so the common path must be a
    dictionary read -- not a Key Vault round trip per call."""
    calls = redemption(monkeypatch)

    tokens = [graph_auth.get_access_token() for _ in range(50)]

    assert len(calls) == 1, "the token should have been redeemed exactly once"
    assert len(set(tokens)) == 1


def test_a_token_near_expiry_is_refreshed_exactly_once(monkeypatch, vault):
    calls = redemption(monkeypatch, expires_in=3600)
    first = graph_auth.get_access_token()

    # Jump to inside the refresh margin. The real clock is captured BEFORE
    # patching -- a lambda that called time.time() after the patch would call
    # itself.
    real_time = time.time
    jumped = real_time() + 3600 - graph_auth.REFRESH_MARGIN_SECONDS + 1
    monkeypatch.setattr(graph_auth.time, "time", lambda: jumped)

    second = graph_auth.get_access_token()
    third = graph_auth.get_access_token()

    assert len(calls) == 2, "expected one refresh, not one per call"
    assert second == third != first


def test_reset_cache_forces_reacquisition(monkeypatch, vault):
    calls = redemption(monkeypatch)
    graph_auth.get_access_token()
    graph_auth.reset_cache()
    graph_auth.get_access_token()

    assert len(calls) == 2


# --- rotation -----------------------------------------------------------------


def test_the_rotated_refresh_token_is_written_back(monkeypatch, vault):
    """Entra returns a new refresh token on every redemption. Not persisting it
    means the stored one ages out and the pipeline stops for no visible reason."""
    redemption(monkeypatch)

    graph_auth.get_access_token()

    assert vault["writes"] == ["refresh-token-v2"]
    assert vault["secret"] == "refresh-token-v2"


def test_each_refresh_redeems_the_most_recently_stored_token(monkeypatch, vault):
    calls = redemption(monkeypatch, expires_in=1)
    graph_auth.get_access_token()
    graph_auth.reset_cache()
    graph_auth.get_access_token()

    assert calls == ["refresh-token-v1", "refresh-token-v2"]


def test_a_response_without_a_new_refresh_token_is_not_a_failure(monkeypatch, vault):
    """Nothing to write back is fine -- the stored token stays valid, because
    Entra does not revoke the old one on rotation."""
    redemption(monkeypatch, rotate=False)

    assert graph_auth.get_access_token() == "access-1"
    assert vault["writes"] == []


def test_a_key_vault_write_failure_does_not_fail_the_request(monkeypatch):
    """The access token we just obtained is valid either way, and the STORED
    refresh token stays valid too because Entra does not revoke it on rotation.
    Failing the print run because we could not file the next token away would
    turn a warning into an outage.

    This exercises the real write_refresh_token, not the vault fixture, so the
    swallow is genuinely under test.
    """
    monkeypatch.setattr(graph_auth, "read_refresh_token", lambda: "refresh-token-v1")

    def unreachable_vault():
        raise RuntimeError("vault unreachable")

    monkeypatch.setattr(graph_auth, "_secret_client", unreachable_vault)
    calls = redemption(monkeypatch)

    token = graph_auth.get_access_token()

    assert token == "access-1", "a vault write failure must not fail the request"
    assert len(calls) == 1


def test_the_bootstrap_write_raises_where_the_rotation_write_swallows(monkeypatch, caplog):
    """Same Key Vault write, opposite failure policy. The runtime's rotation must
    not fail a print run over the NEXT token; the bootstrap's whole job is the
    write, so its failure has to surface."""
    def unreachable_vault():
        raise RuntimeError("vault unreachable")

    monkeypatch.setattr(graph_auth, "_secret_client", unreachable_vault)

    with pytest.raises(RuntimeError, match="vault unreachable"):
        graph_auth.store_refresh_token("refresh-token-v2")
    with caplog.at_level(logging.WARNING):
        graph_auth.write_refresh_token("refresh-token-v2")  # raises nothing
    assert "could not write the rotated refresh token back to Key Vault" in caplog.text


def test_the_rotation_write_reaches_the_vault(monkeypatch):
    """The vault fixture replaces write_refresh_token wholesale, so this is the
    test that the real one still stores the rotated token."""
    monkeypatch.delenv("PRINT_REFRESH_TOKEN_SECRET", raising=False)
    writes = []

    class FakeSecretClient:
        def set_secret(self, name, value):
            writes.append((name, value))
            return SimpleNamespace(properties=SimpleNamespace(version="v2", created_on=None))

    monkeypatch.setattr(graph_auth, "_secret_client", FakeSecretClient)

    graph_auth.write_refresh_token("refresh-token-v2")

    assert writes == [("up-print-refresh-token", "refresh-token-v2")]


# --- a dead token names its own fix ------------------------------------------


@pytest.mark.parametrize("error", ["invalid_grant", "interaction_required",
                                   "invalid_client"])
def test_a_revoked_token_asks_for_the_bootstrap_script(monkeypatch, vault, error):
    """A password change, an SSPR, an admin reset, an explicit revocation or an
    MFA that has aged out (AADSTS50078) all land here, and none of them can be
    fixed in code."""
    import msal

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def acquire_token_by_refresh_token(self, refresh_token, scopes):
            return {"error": error, "error_description": "token is dead"}

    monkeypatch.setattr(msal, "PublicClientApplication", FakeApp)

    with pytest.raises(graph_auth.AuthBootstrapRequired) as excinfo:
        graph_auth.get_access_token()

    assert "bootstrap_token.py" in str(excinfo.value)


def test_an_unexpected_token_error_is_not_mistaken_for_a_dead_token(monkeypatch, vault):
    """A transient service error must not send someone off to re-run an
    interactive sign-in they did not need."""
    import msal

    class FakeApp:
        def __init__(self, **kwargs):
            pass

        def acquire_token_by_refresh_token(self, refresh_token, scopes):
            return {"error": "temporarily_unavailable", "error_description": "try later"}

    monkeypatch.setattr(msal, "PublicClientApplication", FakeApp)

    with pytest.raises(RuntimeError) as excinfo:
        graph_auth.get_access_token()

    assert not isinstance(excinfo.value, graph_auth.AuthBootstrapRequired)


def test_the_endpoint_surfaces_the_remedy(graph, monkeypatch):
    """The route turns it into a 500 whose body says exactly what to do, so
    nobody spends an hour diagnosing an expired credential."""
    def boom():
        raise graph_auth.AuthBootstrapRequired("the stored refresh token is no "
                                               "longer valid (invalid_grant)")

    monkeypatch.setattr(graph_auth, "get_access_token", boom)

    response = post(SUBMIT, {**SITE, "library": "Documents", "folder": "/x",
                             "printerShareId": "share-guid"})
    payload = as_json(response)

    assert response.status_code == 500
    assert payload["remedy"] == "run scripts/bootstrap_token.py"


# --- the bootstrap ------------------------------------------------------------

BOOTSTRAP_VAULT = "https://kv-bootstrap.vault.azure.net/"


class FakeDeviceFlowApp:
    """msal.PublicClientApplication for the bootstrap: the device-code sign-in
    always succeeds and hands back a refresh token."""

    def __init__(self, **kwargs):
        pass

    def initiate_device_flow(self, scopes):
        return {"user_code": "ABC123", "message": "enter ABC123 at the device login page"}

    def acquire_token_by_device_flow(self, flow):
        return {"access_token": "access-1", "refresh_token": "refresh-token-new",
                "id_token_claims": {"preferred_username": "print@contoso.example"}}


@pytest.fixture
def bootstrap(monkeypatch):
    """bootstrap_token.main() offline: no local settings file, no real sign-in,
    and the vault named on the command line, as an operator would."""
    import msal

    # main() and the module's first import write os.environ directly (--vault,
    # --secret, the REQUESTS_CA_BUNDLE pop); restore all of it afterwards.
    with mock.patch.dict(os.environ):
        import bootstrap_token

        monkeypatch.delenv("PRINT_REFRESH_TOKEN_SECRET", raising=False)
        monkeypatch.setattr(bootstrap_token, "_load_local_settings", lambda: None)
        monkeypatch.setattr(msal, "PublicClientApplication", FakeDeviceFlowApp)
        monkeypatch.setattr(sys, "argv", ["bootstrap_token.py", "--vault", BOOTSTRAP_VAULT])
        yield bootstrap_token.main


def test_the_bootstrap_stops_when_the_vault_write_fails(bootstrap, monkeypatch, capsys):
    """It used to call the best-effort write_refresh_token, so a failed write --
    a placeholder vault URL, a credential that cannot reach the vault -- logged a
    warning and then printed "stored refresh token" anyway, leaving the Function
    App on the dead token. Found 2026-10-04, with this repo's placeholder
    KEY_VAULT_URI still in a local.settings.json."""
    def unreachable_vault():
        raise RuntimeError("vault unreachable")

    monkeypatch.setattr(graph_auth, "_secret_client", unreachable_vault)

    with pytest.raises(SystemExit) as excinfo:
        bootstrap()

    message = str(excinfo.value.code)
    captured = capsys.readouterr()
    assert "NOTHING WAS WRITTEN" in message
    assert BOOTSTRAP_VAULT in message
    assert "vault unreachable" in message
    assert "stored refresh token" not in captured.out
    assert "refresh-token-new" not in message + captured.out + captured.err


def test_the_bootstrap_reports_the_version_it_stored(bootstrap, monkeypatch, capsys):
    """The success line names the new version, so it can be matched against the
    vault's Versions blade instead of being taken on trust."""
    writes = []

    class FakeSecretClient:
        def set_secret(self, name, value):
            writes.append((name, value))
            return SimpleNamespace(properties=SimpleNamespace(
                version="v-new", created_on="2026-10-05 03:00:00+00:00"))

    monkeypatch.setattr(graph_auth, "_secret_client", FakeSecretClient)

    assert bootstrap() == 0
    assert writes == [("up-print-refresh-token", "refresh-token-new")]
    captured = capsys.readouterr()
    assert ("stored refresh token in secret 'up-print-refresh-token' at "
            + BOOTSTRAP_VAULT) in captured.out
    assert "version v-new" in captured.out
    assert "refresh-token-new" not in captured.out + captured.err


# --- required configuration ---------------------------------------------------


@pytest.mark.parametrize("name,accessor", [
    ("GRAPH_TENANT_ID", graph_auth.tenant_id),
    ("GRAPH_CLIENT_ID", graph_auth.client_id),
    ("KEY_VAULT_URI", graph_auth.key_vault_uri),
])
def test_environment_naming_config_has_no_default(monkeypatch, name, accessor):
    """A silent fallback could point the app at the wrong tenant or vault. These
    must raise, not guess (playbook §2.7)."""
    monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match=name):
        accessor()


def test_the_scopes_exclude_the_reserved_ones():
    """MSAL adds offline_access / openid / profile itself and rejects them if
    they are passed explicitly."""
    for reserved in ("offline_access", "openid", "profile"):
        assert reserved not in graph_auth.SCOPES


def test_the_scopes_cover_every_operation_the_app_performs():
    """One assertion per call the app makes, because this test previously said
    "every operation" while omitting one -- and the one it omitted was the one
    ReadWriteBasic does not cover.

    createUploadSession accepts PrintJob.Create or PrintJob.ReadWrite, and NOT
    PrintJob.ReadWriteBasic (verified against the v1.0 permission table,
    2026-09-01). Dropping PrintJob.Create returns the pipeline to defect L3: a
    403 arriving after the row is claimed and the job created.
    """
    joined = " ".join(graph_auth.SCOPES)
    assert "Sites.ReadWrite.All" in joined      # read the queue, write the columns
    assert "PrintJob.ReadWriteBasic" in joined  # create the job, start it, cancel it
    assert "PrintJob.Create" in joined          # createUploadSession -- Basic is refused
    assert "Printer.Read.All" in joined         # resolve the printer behind a share
