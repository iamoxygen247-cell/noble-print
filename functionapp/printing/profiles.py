"""
profiles.py — one profile per printer capability shape.

A profile is the pairing of a **document conversion** with the **job
configuration that makes its output print correctly**. Those two are not
separable, and the reason is worth stating because it is easy to undo:

    pwg_converter renders each page onto a full-bleed canvas the exact size of
    the media -- US Letter at 300 dpi is always 2550x3300 -- and deliberately
    does NOT inset the printer's unprintable margins. The job configuration
    compensates with `scaling: fit` plus the device's margins, so the printer
    shrinks the full page into its printable area. Send that raster with a
    different scaling, or with no margins, and the page prints cropped or
    scaled even though the file is valid.

That coupling is why the configuration lives here beside the converter rather
than in print_policy.JOB_CONFIGURATION, which stays the minimal default for
printers that need no conversion at all.

Adding a printer means adding a profile class and registering it in
__init__.PROFILES. The pipeline does not change.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import print_policy

from . import pwg_converter

# --- MIME types ---------------------------------------------------------------
# universal_print.DEFAULT_CONTENT_TYPE already owns "application/pdf" for the
# transport layer; these name the conversion endpoints.
PDF = "application/pdf"
PWG_RASTER = "image/pwg-raster"

# --- raster tunables ----------------------------------------------------------
DEFAULT_RASTER_DPI = 300
MIN_RASTER_DPI = 72
MAX_RASTER_DPI = 1200

# A Letter page at 300 dpi measures ~1.4 MB after PWG's run-length encoding, so
# this leaves room for a long invoice while still failing a runaway document
# before it exhausts a 2 GB instance. pwg_converter has its own, much larger,
# internal ceiling; this is the operational one.
DEFAULT_MAX_OUTPUT_BYTES = 32 * 1024 * 1024

# Fallback only. The Brother MFC-L5800DW reports 4320 microns on every edge and
# that is the value the proof of concept printed with; it is used when a share
# reports no margins of its own.
FALLBACK_MARGIN_MICRONS = 4320


class ProfileError(RuntimeError):
    """The document cannot be prepared for this printer."""


def _tunable_int(env_var: str, default: int, low: int, high: int) -> int:
    """Same shape as print_policy._resolve_int's env branch: an out-of-range
    server value warns and falls back rather than taking the run down."""
    raw = os.getenv(env_var)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logging.warning("%s=%s is not an integer; using %s", env_var, raw, default)
        return default
    if not low <= value <= high:
        logging.warning("%s=%s is outside [%s, %s]; using %s",
                        env_var, raw, low, high, default)
        return default
    return value


def _first_supported(offered: Optional[List[Any]], preferred: Any) -> Any:
    """Prefer `preferred` when the printer reports it; otherwise take what the
    printer offers. An empty list means the printer reported nothing, which is
    treated as "unknown", not as "refuses" -- the same benefit of the doubt
    universal_print.ShareInfo.supports() gives content types."""
    if not offered or preferred in offered:
        return preferred
    return offered[0]


class PassthroughProfile:
    """The printer already accepts the document. Send it untouched.

    This is the behaviour the pipeline had before any conversion existed, kept
    as a profile so that the passthrough case travels through exactly the same
    code path as a converting one.
    """

    name = "passthrough"

    def matches(self, share: Any, source_content_type: str) -> bool:
        return bool(share.supports(source_content_type))

    def target_content_type(self, source_content_type: str) -> str:
        return source_content_type

    def convert(self, data: bytes, share: Any) -> bytes:
        return data

    def job_configuration(self, share: Any) -> Dict[str, Any]:
        # print_policy owns the minimal default and imports nothing but the
        # standard library, so there is no cycle to worry about here.
        return dict(print_policy.JOB_CONFIGURATION)


class PwgRasterProfile:
    """PDF -> image/pwg-raster, for a printer that reports PWG raster only.

    Verified end to end on a Brother MFC-L5800DW series [3c2af401eecf]: a
    2550x3300 sgray_8 raster at 300 dpi, 1,423,827 bytes for a one-page
    invoice, printed correctly with the configuration below.
    """

    name = "pdf-to-pwg-raster"

    def matches(self, share: Any, source_content_type: str) -> bool:
        if share.supports(source_content_type):
            return False        # passthrough is cheaper; let it win
        if (source_content_type or "").split(";")[0].strip().lower() != PDF:
            return False
        return PWG_RASTER in [str(t).split(";")[0].strip().lower()
                              for t in (share.content_types or [])]

    def target_content_type(self, source_content_type: str) -> str:
        return PWG_RASTER

    # -- conversion ------------------------------------------------------------

    def dpi(self, share: Any) -> int:
        wanted = _tunable_int("PRINT_RASTER_DPI", DEFAULT_RASTER_DPI,
                              MIN_RASTER_DPI, MAX_RASTER_DPI)
        offered = list(getattr(share, "dpis", None) or [])
        if offered and wanted not in offered:
            # Take the highest resolution at or below the request, so a printer
            # that cannot do 300 gets the best it can rather than a failure.
            lower = [d for d in sorted(offered) if d <= wanted]
            chosen = lower[-1] if lower else min(offered)
            logging.warning("printer does not offer %s dpi (offers %s); using %s",
                            wanted, offered, chosen)
            return int(chosen)
        return wanted

    def convert(self, data: bytes, share: Any) -> bytes:
        ceiling = _tunable_int("PRINT_RASTER_MAX_BYTES", DEFAULT_MAX_OUTPUT_BYTES,
                               1024, pwg_converter.MAX_OUTPUT_BYTES)
        try:
            raster = pwg_converter.convert_pdf(data, dpi=self.dpi(share))
        except pwg_converter.ConversionError as exc:
            raise ProfileError(str(exc)) from exc

        if len(raster) > ceiling:
            raise ProfileError(
                f"rasterized document is {len(raster):,} bytes, over the "
                f"{ceiling:,}-byte limit; lower PRINT_RASTER_DPI or split the file")
        return raster

    # -- the configuration that makes that raster print correctly --------------

    def _margins(self, share: Any) -> Dict[str, int]:
        """Microns per edge, from the printer when it reports them."""
        def edge(attribute: str) -> int:
            values = list(getattr(share, attribute, None) or [])
            return int(max(values)) if values else FALLBACK_MARGIN_MICRONS

        return {
            "top": edge("top_margins"),
            "bottom": edge("bottom_margins"),
            "left": edge("left_margins"),
            "right": edge("right_margins"),
        }

    def job_configuration(self, share: Any) -> Dict[str, Any]:
        media = _first_supported(list(getattr(share, "media_sizes", None) or []),
                                 "North America Letter")
        scaling = _first_supported(list(getattr(share, "scalings", None) or []),
                                   "fit")
        return {
            "copies": 1,
            "dpi": self.dpi(share),
            "orientation": "portrait",
            "duplexMode": "oneSided",
            "colorMode": "grayscale",
            "inputBin": "auto",
            "outputBin": "face-down",
            "mediaSize": media,
            "mediaType": "stationery",
            "quality": "medium",
            # `fit` is load-bearing, not cosmetic -- see the module docstring.
            "scaling": scaling,
            "margin": self._margins(share),
        }
