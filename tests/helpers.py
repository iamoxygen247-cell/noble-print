"""
helpers.py — invoking the HTTP routes offline.

The Azure Functions v2 model wraps each route in a FunctionBuilder; calling
.build().get_user_function() hands back the plain function underneath, so the
whole request path runs in-process with no Functions host, no ports and no
network. This is the same technique the sibling invoice project uses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import azure.functions as func

import function_app

from datetime import datetime, timedelta, timezone

# The suite's fixed clock. print_policy.now_utc is pinned to this by the
# frozen_now fixture, so every window assertion is deterministic.
NOW = datetime(2026, 8, 30, 21, 0, 0, tzinfo=timezone.utc)

# The site every offline test addresses. This used to be two app settings, set by
# conftest's clean_env fixture; it is request input now, so it lives here and each
# module's body constant spreads it. One definition, so the day the key names change
# there is one place to change them -- and a body that omits it gets a 400 rather
# than quietly addressing whatever the environment happened to hold.
SITE = {"sharepointHostname": "contoso.sharepoint.com",
        "sharepointSitePath": "/sites/Ops"}


def iso(*, days: float = 0, hours: float = 0, minutes: float = 0) -> str:
    """A timestamp that far before NOW.

    Used for both SharePoint's createdDateTime and printJob.createdDateTime.
    `minutes` matters for the retry schedule, whose first boundaries are five and
    fifteen minutes apart.
    """
    return (NOW - timedelta(days=days, hours=hours, minutes=minutes)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _handler(name: str):
    return getattr(function_app, name).build().get_user_function()


SUBMIT = _handler("submit_print_jobs")
POLL = _handler("poll_print_status")
HEALTH = _handler("check_printer_health")


def post(handler, payload: Any) -> func.HttpResponse:
    body = (payload if isinstance(payload, (bytes, bytearray))
            else json.dumps(payload).encode("utf-8"))
    request = func.HttpRequest(
        method="POST",
        url="http://localhost/api/print/test",
        headers={"content-type": "application/json"},
        params={},
        body=body,
    )
    return handler(request)


def as_json(response: func.HttpResponse) -> dict:
    return json.loads(response.get_body().decode("utf-8"))


def result_for(payload: dict, item_id: str) -> Dict[str, Any]:
    """The per-item record for one file out of a response."""
    for item in payload.get("items", []):
        if item.get("itemId") == item_id:
            return item
    raise AssertionError("no record for item {} in {}".format(item_id, payload))


def print_events(caplog) -> List[Dict[str, str]]:
    """Parse the PRINT_EVENT lines out of captured logs.

    The weekly reporting in docs/design.md §13 is built entirely on these lines,
    so the tests assert them the same way the KQL does: by parsing the message
    text. If the field order or names drift, the workbook silently reports
    nothing and these assertions are what catch it.
    """
    events = []
    for record in caplog.records:
        message = record.getMessage()
        if not message.startswith("PRINT_EVENT "):
            continue
        fields: Dict[str, str] = {}
        # file= is last and may contain spaces, so it is split off first.
        head, _, file_name = message.partition(" file=")
        for token in head[len("PRINT_EVENT "):].split(" "):
            key, _, value = token.partition("=")
            fields[key] = value
        fields["file"] = file_name
        events.append(fields)
    return events


def run_summaries(caplog) -> List[Dict[str, str]]:
    summaries = []
    for record in caplog.records:
        message = record.getMessage()
        if not message.startswith("RUN_SUMMARY "):
            continue
        fields: Dict[str, str] = {}
        head, _, folder = message.partition(" folder=")
        for token in head[len("RUN_SUMMARY "):].split(" "):
            key, _, value = token.partition("=")
            fields[key] = value
        fields["folder"] = folder
        summaries.append(fields)
    return summaries
