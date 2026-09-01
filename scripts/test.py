"""
test.py — the one client harness, for the local host and the deployed app alike.

    # local (func start), no key needed
    .\\.venv\\Scripts\\python.exe scripts\\test.py dryrun --library "Documents" \\
        --folder "/Invoices/ToPrint" --printer-share-id <guid>

    # deployed -- same script, same flags
    .\\.venv\\Scripts\\python.exe scripts\\test.py submit --base-url https://<HOST> \\
        --key <FUNCTION_KEY> --library "Documents" --folder "/Invoices/ToPrint" \\
        --printer-share-id <guid> --batch-size 1

ONE harness for both targets, deliberately (playbook §6.5): two of them drift,
and the local one becomes the one that lies. --base-url defaults to the local
host so the common case needs no flags.

STANDARD LIBRARY ONLY -- it runs anywhere with no install step. It is not
collected by pytest (pytest.ini scopes testpaths to tests/); it talks to a
running host, which is exactly what the offline suite does not.

Note that dryrun/stale are MODES OF THE ENDPOINTS, not logic in this file. A
harness with its own code path proves nothing about the deployed app; going
through the real endpoint exercises the real auth, resolution and queries.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://localhost:7071"

ROUTES = {
    "submit": "/api/print/submit",
    "dryrun": "/api/print/submit",
    "status": "/api/print/status",
    "stale": "/api/print/status",
    "resubmit": "/api/print/resubmit",
}

# Windows redirects stdout as cp1252, so a file name with an accent raises
# UnicodeEncodeError and buries the real output under a traceback (playbook §5.3).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


def call(base_url: str, route: str, key: str, body: dict, timeout: float = 180.0):
    url = base_url.rstrip("/") + route
    if key:
        url += "?" + urllib.parse.urlencode({"code": key})

    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"error": raw}
    except urllib.error.URLError as exc:
        raise SystemExit(
            "could not reach {}: {}\n"
            "If you meant the local host, is `func start` running? "
            "See scripts/start-local.ps1.".format(url, exc.reason))


def summarise(command: str, status: int, payload: dict) -> None:
    print("HTTP {}".format(status))

    if command == "dryrun" and payload.get("dryRun"):
        print("library      : {}".format(payload.get("library")))
        print("folder       : {}".format(payload.get("folder")))
        print("columns      :")
        for display, internal in (payload.get("resolvedColumns") or {}).items():
            flag = "  (encoded)" if display != internal else ""
            print("    {:<14} -> {}{}".format(display, internal, flag))
        printer = payload.get("printer") or {}
        print("printer      : {} ({})".format(printer.get("displayName"),
                                              printer.get("shareId")))
        print("  printer id : {}   <- cancel uses this, not the share id".format(
            printer.get("printerId")))
        print("  accepting  : {}".format(printer.get("acceptingJobs")))
        print("  content    : {}".format(", ".join(printer.get("contentTypes") or [])))
        if printer.get("dpis"):
            print("  dpis       : {}".format(
                ", ".join(str(d) for d in printer["dpis"])))

        # Which printer profile would run. A printer that needs rasterizing says
        # so here, before any file is queued -- rather than after every file has
        # failed at preflight.
        conversion = payload.get("conversion") or {}
        if conversion:
            if not conversion.get("supported"):
                print("conversion   : NONE -- this printer accepts nothing we can "
                      "produce. Every file would fail.")
            else:
                print("conversion   : {}{}".format(
                    conversion.get("profile"),
                    "" if conversion.get("uploadContentType") ==
                          conversion.get("sourceContentType")
                    else "  ({} -> {})".format(conversion.get("sourceContentType"),
                                               conversion.get("uploadContentType"))))
                job = conversion.get("jobConfiguration") or {}
                if job:
                    print("  job config : {}".format(
                        ", ".join("{}={}".format(k, v) for k, v in sorted(job.items())
                                  if not isinstance(v, dict))))

        print("candidates   : {}".format(payload.get("candidatesFound")))
        for item in payload.get("wouldSubmit") or []:
            print("    would submit {} ({})".format(item["itemId"], item["fileName"]))
        if not printer.get("acceptingJobs"):
            print("\nWARNING: the printer is not accepting jobs; a real run "
                  "would submit nothing.")
        return

    if command == "stale":
        count = payload.get("staleCount", 0)
        print("stale (past the {}-day window): {}".format(
            payload.get("windowDays"), count))
        for item in payload.get("staleItems") or []:
            print("    {} {}  created {}  job {}".format(
                item["itemId"], item["fileName"], item["created"],
                item["jobId"] or "-"))
        if count:
            print("\nThese are excluded by BOTH status and resubmit, so nothing "
                  "will touch them again. Handle them by hand.")
        return

    for key in ("candidatesFound", "remainingReady", "submitted", "failed",
                "skipped", "checked", "completed", "stillRunning", "notFound",
                "malformed", "awaitingResubmit", "uncheckedCount", "staleCount",
                "resubmitted", "completedInstead", "cancelled",
                "printerAvailable", "budgetExhausted"):
        if key in payload:
            print("{:<16}: {}".format(key, payload[key]))

    for item in payload.get("items") or []:
        print("  {:<8} {:<10} {}".format(
            item.get("itemId", "?"), item.get("result", "?"),
            item.get("message") or item.get("jobId") or item.get("fileName") or ""))
        # A job that printed but whose id could not be recorded. The document is
        # on paper; SharePoint does not know it, so Resubmit will print it again.
        # Nothing else in this output would tell you.
        if item.get("warning"):
            print("    !! {}".format(item["warning"]))

    if payload.get("printerAvailable") is False:
        print("\nWARNING: the printer is not accepting jobs, so nothing was "
              "submitted. This is a 200 with failed=0 -- Flow A only notices it "
              "because it tests printerAvailable.")
    if payload.get("budgetExhausted"):
        print("\nNOTE: the wall-clock budget was spent before the batch finished. "
              "Anything left is reported above and picked up on the next run.")

    if payload.get("error"):
        print("error   : {}".format(payload["error"]))
    if payload.get("remedy"):
        print("remedy  : {}".format(payload["remedy"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(ROUTES) + ["badpayload"])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="default: the local host, {}".format(DEFAULT_BASE_URL))
    parser.add_argument("--key", default="", help="function key (deployed app only)")
    parser.add_argument("--library", default="Documents")
    parser.add_argument("--folder", default="")
    parser.add_argument("--printer-share-id", default="")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--window-days", type=int)
    parser.add_argument("--min-age-hours", type=int)
    parser.add_argument("--json", action="store_true", help="print the raw response")
    args = parser.parse_args()

    if args.command == "badpayload":
        # The validation path: it must 400 BEFORE anything is claimed, or a
        # malformed call would lock files out until its own retry.
        status, payload = call(args.base_url, ROUTES["submit"], args.key,
                               {"library": "Documents"})
        print("HTTP {} (expected 400)".format(status))
        print(json.dumps(payload, indent=2))
        if status != 400:
            print("\nFAIL: a malformed request must be rejected with 400.")
            return 1
        print("\nOK: rejected without touching the queue.")
        return 0

    body = {"library": args.library, "folder": args.folder}
    if args.command in ("submit", "dryrun", "resubmit") and args.printer_share_id:
        body["printerShareId"] = args.printer_share_id
    if args.command == "dryrun":
        body["dryRun"] = True
    if args.batch_size is not None:
        body["batchSize"] = args.batch_size
    if args.window_days is not None:
        body["windowDays"] = args.window_days
    if args.min_age_hours is not None:
        body["minAgeHours"] = args.min_age_hours

    if args.command in ("submit", "dryrun") and not args.printer_share_id:
        parser.error("--printer-share-id is required for {}".format(args.command))

    status, payload = call(args.base_url, ROUTES[args.command], args.key, body)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        summarise(args.command, status, payload)

    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
