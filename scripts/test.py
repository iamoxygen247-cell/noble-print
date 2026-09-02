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

Note that dryrun is a MODE OF THE ENDPOINT, not logic in this file. A harness
with its own code path proves nothing about the deployed app; going through the
real endpoint exercises the real auth, resolution and queries.

The retry knobs -- --stall-minutes and --give-up-days -- go into the request body
exactly as Power Automate sends them, which is how the pacing is retuned without a
deploy. Use them here to prove a change before putting it in a flow. The same is
true of --batch-size, --print-format and, on `status`, --printer-share-id.

--hostname and --site-path are REQUIRED for submit, dryrun and status: the site
moved out of app settings and into the request, so this script has to name it the
same way a flow does. `health` needs neither -- it reads a printer share and no
SharePoint list at all.

    # upload a raster instead of the PDF, without touching the printer's config
    ... submit --printer-share-id <guid> --print-format image/pwg-raster

    # poll every row against ONE printer, whatever its Printer_Name says
    ... status --printer-share-id <guid>

--print-format and --printer-share-id are both OPT-IN: omit them and you get
exactly the behaviour that existed before they did.
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
    "health": "/api/print/health",
}

# Endpoints that address a printer rather than the library. `health` takes ONLY
# the printer, so sending library/folder would be noise the endpoint ignores.
PRINTER_ONLY = ("health",)

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
            # Echoed by the endpoint. Printed on its own line because it is the
            # ONLY way to see that --print-format actually reached the endpoint:
            # ask for application/pdf on a printer that takes PDF and every other
            # line looks exactly like a run with no flag at all.
            requested = conversion.get("requestedFormat")
            print("requested fmt: {}".format(
                requested or "(none -- the printer's capabilities choose)"))

            if not conversion.get("supported"):
                if requested:
                    print("conversion   : NONE -- {} cannot be produced from a {} "
                          "document. Every file would fail.".format(
                              requested, conversion.get("sourceContentType")))
                else:
                    print("conversion   : NONE -- this printer accepts nothing we "
                          "can produce. Every file would fail.")
            else:
                print("conversion   : {}{}".format(
                    conversion.get("profile"),
                    "" if conversion.get("uploadContentType") ==
                          conversion.get("sourceContentType")
                    else "  ({} -> {})".format(conversion.get("sourceContentType"),
                                               conversion.get("uploadContentType"))))
                print("  converts   : {}".format(
                    conversion.get("conversionRequired")))
                job = conversion.get("jobConfiguration") or {}
                if job:
                    print("  job config : {}".format(
                        ", ".join("{}={}".format(k, v) for k, v in sorted(job.items())
                                  if not isinstance(v, dict))))

        # Two different empties: nothing to print, and nothing DUE to print. A dry
        # run that reported only the first would look identical for a queue full of
        # files waiting out their retry backoff.
        print("candidates   : {} due, {} not yet due".format(
            payload.get("candidatesFound"), payload.get("notYetDue")))
        for item in payload.get("wouldSubmit") or []:
            print("    would submit {} ({}) print_time={}".format(
                item["itemId"], item["fileName"], item.get("printTime")))
        if not printer.get("acceptingJobs"):
            print("\nWARNING: the printer is not accepting jobs; a real run "
                  "would submit nothing.")
        return

    if command == "health":
        # `healthy` is on every 200 by contract, so this never needs a guard --
        # the same reason a Power Automate condition does not.
        print("healthy      : {}".format(payload.get("healthy")))
        print("printer      : {}".format(payload.get("printerShareId")))
        print("format       : {}".format(
            payload.get("printFormat") or "(none -- capabilities choose)"))
        if payload.get("message"):
            print("summary      : {}".format(payload["message"]))

        for label, key in (("ERROR  ", "errors"), ("warning", "warnings")):
            for finding in payload.get(key) or []:
                print("  {} {}: {}".format(label, finding.get("code"),
                                           finding.get("message")))
                if finding.get("remedy"):
                    print("           -> {}".format(finding["remedy"]))

        printer = payload.get("printer") or {}
        if printer:
            print("  accepting  : {}   state: {}".format(
                printer.get("acceptingJobs"), printer.get("state") or "(none)"))
            print("  content    : {}".format(
                ", ".join(printer.get("contentTypes") or []) or "(none reported)"))
        conversion = payload.get("conversion") or {}
        if conversion.get("profile"):
            print("  profile    : {}  (converts: {})".format(
                conversion["profile"], conversion.get("conversionRequired")))

        if not payload.get("healthy"):
            print("\nFlow A should NOT submit while this is false. Flow B may "
                  "still poll -- Poll marks completions and gives up on old rows "
                  "whether or not the printer is well.")
        return

    if command == "status" and "stallMinutes" in payload:
        # Echoed by the endpoint, so this shows what was ACTUALLY in force rather
        # than what the caller believes it sent -- the difference matters once the
        # numbers live in a Power Automate flow instead of the code.
        print("settings        : stall={}min giveUp={}d".format(
            payload.get("stallMinutes"), payload.get("giveUpDays")))
        if payload.get("printerShareId"):
            print("printer override: {}".format(payload["printerShareId"]))

    for key in ("candidatesFound", "remainingReady", "notYetDue", "submitted",
                "failed", "skipped", "checked", "completed", "requeued", "gaveUp",
                "stillRunning", "notFound", "malformed", "pendingFound",
                "uncheckedCount", "printerOverridden", "printerAvailable",
                "budgetExhausted"):
        if key in payload:
            # 17, not 16: "printerOverridden" is exactly that long and would
            # otherwise be the one line whose colon does not line up.
            print("{:<17}: {}".format(key, payload[key]))

    for item in payload.get("items") or []:
        print("  {:<8} {:<10} {}".format(
            item.get("itemId", "?"), item.get("result", "?"),
            item.get("message") or item.get("jobId") or item.get("fileName") or ""))
        # A job that printed but whose id could not be recorded. The document is
        # on paper; SharePoint does not know it, so Poll reads the row as a
        # crashed submission and requeues it within minutes. Nothing else in this
        # output would tell you.
        if item.get("warning"):
            print("    !! {}".format(item["warning"]))

    if payload.get("printerAvailable") is False:
        print("\nWARNING: the printer is not accepting jobs, so nothing was "
              "submitted. This is a 200 with failed=0 -- Flow A only notices it "
              "because it tests printerAvailable.")
    if payload.get("printerOverridden"):
        # Not an error -- but it is the exposure the override carried, and the
        # only place anyone would notice it before a duplicate lands in the tray.
        print("\nWARNING: {} row(s) name a DIFFERENT printer from the "
              "--printer-share-id override. Job ids are per-printer, so those "
              "jobs cannot be cancelled from the overriding printer and may "
              "print alongside their replacements (defect F3-R). Expect 0 with "
              "one printer registered.".format(payload["printerOverridden"]))
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
    # The site, which is request input now rather than an app setting. No default:
    # it names an environment, and guessing one would point a real run at whatever
    # tenant happened to be typed into this file.
    parser.add_argument("--hostname", default="",
                        help="submit/dryrun/status: SharePoint hostname, "
                             "e.g. contoso.sharepoint.com")
    parser.add_argument("--site-path", default=None,
                        help='submit/dryrun/status: server-relative site path, '
                             'e.g. /sites/Ops. Pass "" for the root site.')
    parser.add_argument("--printer-share-id", default="")
    parser.add_argument("--batch-size", type=int)
    # The retry knobs, sent in the body exactly as a Power Automate flow sends
    # them -- so a value proved here can be pasted straight into the flow.
    parser.add_argument("--give-up-days", type=int,
                        help="status: fail a PRINT_PENDING row older than this")
    parser.add_argument("--stall-minutes", type=int,
                        help="status: a job idle this long counts as stalled")
    parser.add_argument("--print-format",
                        help="submit/dryrun/health: the format to UPLOAD, e.g. "
                             "application/pdf (no conversion -- the invoice "
                             "already is one) or image/pwg-raster (runs the "
                             "converter). Omit to let the printer's capabilities "
                             "choose, which is what happens without this flag.")
    parser.add_argument("--json", action="store_true", help="print the raw response")
    args = parser.parse_args()

    if args.command == "badpayload":
        # The validation path: it must 400 BEFORE anything is claimed, or a
        # malformed call would lock files out until its own retry.
        #
        # Everything EXCEPT folder is supplied, so the 400 can only be about the
        # missing folder. A body that omits several fields would still 400, but on
        # whichever is checked first -- and would keep passing even if folder
        # validation were removed entirely.
        status, payload = call(args.base_url, ROUTES["submit"], args.key,
                               {"sharepointHostname": args.hostname or "example.sharepoint.com",
                                "sharepointSitePath": args.site_path or "",
                                "library": "Documents",
                                "printerShareId": "share-guid"})
        print("HTTP {} (expected 400)".format(status))
        print(json.dumps(payload, indent=2))
        if status != 400:
            print("\nFAIL: a malformed request must be rejected with 400.")
            return 1
        print("\nOK: rejected without touching the queue.")
        return 0

    body = ({} if args.command in PRINTER_ONLY
            else {"sharepointHostname": args.hostname,
                  "sharepointSitePath": args.site_path or "",
                  "library": args.library, "folder": args.folder})
    # Sent for status as well, where it is an OPTIONAL HARD OVERRIDE: every
    # lookup and cancel in the run addresses this share instead of each row's
    # own Printer_Name. Omit it and every row follows its own column.
    if args.printer_share_id:
        body["printerShareId"] = args.printer_share_id
    if args.command == "dryrun":
        body["dryRun"] = True
    if args.batch_size is not None:
        body["batchSize"] = args.batch_size
    if args.give_up_days is not None:
        body["giveUpDays"] = args.give_up_days
    if args.stall_minutes is not None:
        body["stallMinutes"] = args.stall_minutes
    if args.print_format:
        body["printFormat"] = args.print_format

    if args.command in ("submit", "dryrun", "health") and not args.printer_share_id:
        parser.error("--printer-share-id is required for {}".format(args.command))

    # The site is required wherever a library is resolved. Caught here rather than
    # left to the endpoint's 400 so the message names the flag, not the JSON key.
    if args.command not in PRINTER_ONLY and args.command != "badpayload":
        if not args.hostname:
            parser.error("--hostname is required for {} (the site is request "
                         "input now, not an app setting)".format(args.command))
        if args.site_path is None:
            parser.error('--site-path is required for {} (pass "" for the root '
                         'site)'.format(args.command))

    status, payload = call(args.base_url, ROUTES[args.command], args.key, body)

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        summarise(args.command, status, payload)

    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
