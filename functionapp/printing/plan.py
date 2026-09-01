"""
plan.py — say what would be sent to a printer, without sending it.

    python -m printing.plan --capabilities caps.json [--source application/pdf]
                            [--print-format image/pwg-raster]

Reads a printerShare's `capabilities` object exactly as Microsoft Graph returns
it and prints the decision the pipeline would make: which profile runs, what
content type gets uploaded, at what resolution, and the job configuration that
goes with it.

WHY IT EXISTS. The job configuration and the raster are a matched pair -- the
converter renders full-bleed at the media size and relies on `scaling: fit` plus
the device margins to place it. A bench test that hardcodes its own configuration
can therefore print perfectly while the deployed app prints cropped, and nobody
would know until real invoices came out wrong. This module is how a local test
borrows the *app's* answer instead of inventing its own.

Used by scripts/live-print-test.ps1, and useful on its own to find out whether a
printer needs conversion before any file is queued.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Dict

import universal_print

from . import SUPPORTED_PRINT_FORMATS, profile_for


def share_from_capabilities(capabilities: Dict[str, Any],
                            share_id: str = "", printer_id: str = "",
                            display_name: str = "") -> universal_print.ShareInfo:
    """Build the same ShareInfo the preflight builds, from a capabilities blob.

    Kept beside get_share's field mapping deliberately: if one learns a new
    capability the other must too, and having them adjacent makes that visible.
    """
    def ints(name: str):
        return [int(v) for v in (capabilities.get(name) or [])
                if str(v).lstrip("-").isdigit()]

    return universal_print.ShareInfo(
        share_id=share_id,
        printer_id=printer_id,
        display_name=display_name,
        accepting_jobs=True,
        content_types=list(capabilities.get("contentTypes") or []),
        state="",
        dpis=ints("dpis"),
        scalings=[str(v) for v in (capabilities.get("scalings") or [])],
        media_sizes=[str(v) for v in (capabilities.get("mediaSizes") or [])],
        colour_supported=bool(capabilities.get("isColorPrintingSupported")),
        top_margins=ints("topMargins"),
        bottom_margins=ints("bottomMargins"),
        left_margins=ints("leftMargins"),
        right_margins=ints("rightMargins"),
    )


def build_plan(capabilities: Dict[str, Any], source_content_type: str,
               print_format: str = "", **share_fields: str) -> Dict[str, Any]:
    """What the app would do with this document on this printer.

    `print_format` mirrors Submit's `printFormat`: name one and the profile that
    PRODUCES it is chosen, ignoring what the capabilities imply; leave it empty
    and the capabilities decide, as they always did.

    Routed through `profile_for` -- the same function the route uses -- because
    this module exists so a bench test borrows the APP'S answer instead of
    inventing its own. Calling `select_profile` directly here would have made
    that false the moment a flow started sending `printFormat`: on a printer that
    reports both formats the app would rasterize while this reported
    `passthrough`, which is precisely the drift this file was written to prevent.
    """
    share = share_from_capabilities(capabilities, **share_fields)
    profile = profile_for(share, source_content_type, print_format)

    if profile is None:
        if print_format:
            reason = "cannot produce {} from a {} document".format(
                print_format, source_content_type)
        else:
            reason = "printer {} does not accept {} (supports: {})".format(
                share.display_name or share.share_id or "share",
                source_content_type,
                ", ".join(share.content_types) or "unknown")
        return {
            "supported": False,
            "sourceContentType": source_content_type,
            "requestedFormat": print_format or None,
            "reason": reason,
        }

    configuration = profile.job_configuration(share)
    return {
        "supported": True,
        "profile": profile.name,
        "sourceContentType": source_content_type,
        "requestedFormat": print_format or None,
        "uploadContentType": profile.target_content_type(source_content_type),
        # The converter MUST render at this resolution: the job declares it and
        # the page header must agree, or the sheet comes out scaled.
        "rasterDpi": configuration.get("dpi"),
        "conversionRequired": profile.name != "passthrough",
        "jobConfiguration": configuration,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report what would be uploaded to a printer share.")
    parser.add_argument("--capabilities", required=True,
                        help="a JSON file holding the share's capabilities "
                             "object, or - to read stdin")
    parser.add_argument("--source", default=universal_print.DEFAULT_CONTENT_TYPE,
                        help="the document's content type (default: %(default)s)")
    parser.add_argument("--print-format", default="",
                        choices=("",) + tuple(SUPPORTED_PRINT_FORMATS),
                        help="the format to UPLOAD, matching Submit's "
                             "printFormat. Omit to let the printer's "
                             "capabilities choose.")
    parser.add_argument("--share-id", default="")
    parser.add_argument("--printer-id", default="")
    parser.add_argument("--display-name", default="")
    args = parser.parse_args(argv)

    try:
        if args.capabilities == "-":
            raw = sys.stdin.read()
        else:
            raw = pathlib.Path(args.capabilities).read_text(encoding="utf-8-sig")
        capabilities = json.loads(raw)
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not read capabilities: {exc}", file=sys.stderr)
        return 1

    if not isinstance(capabilities, dict):
        print("ERROR: capabilities must be a JSON object", file=sys.stderr)
        return 1

    plan = build_plan(capabilities, args.source, args.print_format,
                      share_id=args.share_id, printer_id=args.printer_id,
                      display_name=args.display_name)
    print(json.dumps(plan, indent=2))
    return 0 if plan.get("supported") else 2


if __name__ == "__main__":
    raise SystemExit(main())
