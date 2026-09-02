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

    Only two settings still influence a run -- the wall-clock budget and the
    display time zone -- and both are cleared so a value left in the developer's
    shell cannot change what the suite asserts.

    The site used to be supplied here, as SHAREPOINT_HOSTNAME and
    SHAREPOINT_SITE_PATH, and the tunables used to be cleared here because each
    had an app-setting fallback. All of that now arrives in the request body, so
    the environment has nothing left to say about which site a run touches or how
    it is paced. The values live in `helpers.SITE` instead.
    """
    for name in ("PRINT_BUDGET_SECONDS", "PRINT_BUSINESS_TZ",
                 "PRINT_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


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


@pytest.fixture(scope="session")
def letter_pdf() -> bytes:
    """A real one-page US Letter PDF, built by scripts/make_test_pdf.py.

    For any test that needs the raster converter to actually succeed. It is NOT
    the pinned fixture: test_printing.py keeps its own, with the exact lines its
    output hash was measured from, because that hash and the text that produced
    it must not drift apart. Use this one when you need a valid PDF and do not
    care what it says.
    """
    import importlib.util

    script = _REPO / "scripts" / "make_test_pdf.py"
    spec = importlib.util.spec_from_file_location("_letter_pdf_builder", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_pdf(["noble-print shared test document"])
