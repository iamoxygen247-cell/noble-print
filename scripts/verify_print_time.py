r"""
verify_print_time.py — prove the Print_Time round trip against the real library.

This is the one thing the offline suite cannot establish. Everything about the
retry schedule is tested against FakeGraph, but FakeGraph stores whatever it is
handed; only SharePoint can answer whether a **Date and Time** column accepts the
ISO-8601 string this app writes, hands it back unchanged, and is emptied by a
JSON null.

Three questions, in order:

    1  does the Print_Time column resolve at all, and to which internal name?
    2  does a written value come back as the SAME INSTANT?
    3  does None actually clear it?

Question 1 is READ ONLY and is all `--item` omitted will do. Questions 2 and 3
need `--item <id>`, and they WRITE TO THAT ROW -- so nominate a row you do not
mind disturbing. The original value is restored afterwards either way, but a
crash between the write and the restore would leave the test value behind, which
is why this refuses to run against a row that is not PRINT_READY unless told to.

    .\.venv\Scripts\python.exe scripts\verify_print_time.py `
        --hostname noblehomes.sharepoint.com --site-path /sites/PM `
        --library "AI_DropBox_V2026"

    ...then, on a row you choose from that output:

    .\.venv\Scripts\python.exe scripts\verify_print_time.py `
        --hostname noblehomes.sharepoint.com --site-path /sites/PM `
        --library "AI_DropBox_V2026" --item 42

Not collected by pytest: it talks to a live tenant, which is exactly what the
offline suite does not.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
from datetime import timedelta

# Corporate TLS inspection makes OpenSSL-based clients fail certificate
# validation while .NET tooling works, because .NET trusts the Windows store.
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
import print_policy  # noqa: E402
import sharepoint  # noqa: E402
from graph_client import GraphClient  # noqa: E402


def _load_local_settings() -> None:
    """Read functionapp/local.settings.json so the script needs no separate
    configuration. utf-8-SIG because Windows tooling writes a BOM."""
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--site-path", required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--item", default="",
                        help="list item id to round-trip. WRITES to this row. "
                             "Omit for the read-only column check.")
    parser.add_argument("--force", action="store_true",
                        help="round-trip a row that is not PRINT_READY")
    args = parser.parse_args()

    _load_local_settings()

    client = GraphClient(graph_auth.get_access_token)
    site_id = sharepoint.resolve_site(client, args.hostname, args.site_path)
    context = sharepoint.resolve_list(client, site_id, args.library)

    # --- 1. the column resolves -----------------------------------------------
    internal = context.internal(print_policy.COLUMN_PRINT_TIME)
    print("1. column resolved")
    print("   {} -> {}".format(print_policy.COLUMN_PRINT_TIME, internal))
    print("   (the app never hardcodes this; SharePoint fixes it at creation)")

    if not args.item:
        ready = sharepoint.query_by_status(client, context, print_policy.READY)
        print("\n   {} PRINT_READY row(s); the first few, with their due times:"
              .format(len(ready)))
        for row in ready[:5]:
            print("     item {:<8} {:<40} Print_Time={!r}".format(
                row.item_id, row.file_name[:40], row.print_time))
        print("\n   Re-run with --item <id> to prove the write round trip.")
        return 0

    # --- 2. a written value comes back as the same instant --------------------
    before = sharepoint.query_by_status(client, context, print_policy.READY)
    row = next((r for r in before if r.item_id == str(args.item)), None)
    if row is None and not args.force:
        print("\nitem {} is not PRINT_READY in this library. Pass --force to "
              "round-trip it anyway.".format(args.item))
        return 1

    original = row.print_time if row else None
    print("\n2. round trip on item {} (original Print_Time: {!r})".format(
        args.item, original))

    # Deliberately a time with a non-zero minute and second, in the future, so a
    # truncated or timezone-shifted value cannot coincidentally match.
    probe = print_policy.now_utc().replace(microsecond=0) + timedelta(
        hours=2, minutes=37, seconds=13)
    written = print_policy.format_business_datetime(probe)
    print("   writing : {}".format(written))
    sharepoint.patch_fields(client, context, str(args.item),
                            {print_policy.COLUMN_PRINT_TIME: written})

    back = _read_one(client, context, str(args.item))
    print("   read back: {!r}".format(back))
    if back == probe:
        print("   PASS -- same instant")
    else:
        drift = (back - probe) if back else None
        print("   FAIL -- differs by {}. If this is a whole number of hours the "
              "offset was lost; check format_business_datetime.".format(drift))

    # --- 3. None clears it ----------------------------------------------------
    print("\n3. clearing with JSON null")
    sharepoint.patch_fields(client, context, str(args.item),
                            {print_policy.COLUMN_PRINT_TIME: None})
    cleared = _read_one(client, context, str(args.item))
    print("   read back: {!r}".format(cleared))
    print("   PASS -- cleared" if cleared is None else
          "   FAIL -- still set. A Date and Time column may need a different "
          "empty value; the app writes None (JSON null).")

    # --- restore --------------------------------------------------------------
    if original is not None:
        sharepoint.patch_fields(
            client, context, str(args.item),
            {print_policy.COLUMN_PRINT_TIME:
                print_policy.format_business_datetime(original)})
        print("\n   original value restored")
    else:
        print("\n   original was empty; left empty")
    return 0


def _read_one(client: GraphClient, context, item_id: str):
    """The one row, straight from Graph, parsed the way the app parses it."""
    payload = client.get_json(
        "{}/{}?$expand=fields".format(context.items_url, item_id)) or {}
    fields = payload.get("fields") or {}
    return print_policy.parse_graph_datetime(
        fields.get(context.internal(print_policy.COLUMN_PRINT_TIME)))


if __name__ == "__main__":
    raise SystemExit(main())
