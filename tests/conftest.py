"""
conftest.py — fixtures shared by the offline suite.

The whole suite runs with no Azure account, no network and no Functions host.
Everything that would leave the process is replaced at the transport boundary,
so the real application code -- adapters, retry loop, orchestration -- executes
unchanged. Only requests.Session and the token provider are fakes.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

# The deployable folder is the import root, exactly as the Functions host sees it
# (playbook §2.1: shared logic lives in one place, and tests reach it via a small
# sys.path bootstrap rather than carrying a copy).
_REPO = pathlib.Path(__file__).resolve().parent.parent
for _candidate in (_REPO / "functionapp", _REPO / "tests"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import graph_auth  # noqa: E402
import graph_client  # noqa: E402
import print_policy  # noqa: E402
from fake_graph import FakeAnonSession, FakeGraph  # noqa: E402

from helpers import NOW, iso  # noqa: E402,F401  -- one definition, shared


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """A known environment for every test.

    Tunables are cleared so a value left in the developer's shell cannot change
    what the suite asserts; the two required settings are supplied so the
    resolve path works without each test repeating them.
    """
    for name in ("PRINT_BATCH_SIZE", "PRINT_GIVE_UP_DAYS", "PRINT_STALL_MINUTES",
                 "PRINT_MAX_RETRIES", "PRINT_BUDGET_SECONDS",
                 "PRINT_BUSINESS_TZ", "PRINT_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHAREPOINT_HOSTNAME", "contoso.sharepoint.com")
    monkeypatch.setenv("SHAREPOINT_SITE_PATH", "/sites/Ops")


@pytest.fixture
def graph(monkeypatch):
    """A FakeGraph wired in as the transport, with a stub token provider.

    Both sessions are replaced: the authenticated one on the GraphClient the
    routes build, and the module-level unauthenticated one used for downloads
    and uploads.
    """
    fake = FakeGraph()
    anon = FakeAnonSession()

    monkeypatch.setattr(graph_auth, "get_access_token", lambda: "fake-access-token")
    monkeypatch.setattr(graph_client, "_anon_session", anon)

    original_init = graph_client.GraphClient.__init__

    # The shim mirrors the real signature exactly, timeout default included:
    # hardcoding 30.0 here would have hidden the fact that GRAPH_TIMEOUT_SECONDS
    # was never resolved at all (S4).
    def patched_init(self, token_provider, timeout=None, session=None):
        # A caller that supplies its own session (a test wiring up a second,
        # differently-configured FakeGraph) keeps it; everything else -- notably
        # the clients the routes build for themselves -- gets this fixture's.
        original_init(self, token_provider, timeout=timeout, session=session or fake)

    monkeypatch.setattr(graph_client.GraphClient, "__init__", patched_init)

    fake.anon = anon
    return fake


@pytest.fixture
def client(graph):
    """A GraphClient bound to the fake transport, for adapter-level tests."""
    return graph_client.GraphClient(lambda: "fake-access-token", session=graph)


@pytest.fixture
def no_sleep(monkeypatch):
    """Make the retry backoff instantaneous, and record what it would have slept
    so a test can still assert the Retry-After was honoured."""
    slept = []
    monkeypatch.setattr(graph_client.time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin print_policy.now_utc so window arithmetic is deterministic."""
    monkeypatch.setattr(print_policy, "now_utc", lambda: NOW)
    return NOW
