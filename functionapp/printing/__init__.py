"""
printing — pluggable per-printer document preparation.

The pipeline pulls a PDF out of SharePoint and hands it to Universal Print. Some
printers take that PDF as-is; others accept only a raster format. Universal Print
will not convert for us -- Microsoft documents exactly one conversion, OXPS to
PDF, and only for printers that already accept PDF -- so the client must produce
whatever the device reports.

This package keeps that per-device knowledge in one place:

    select_profile(share, content_type)               -> what the PRINTER needs
    select_profile_for_format(share, content_type, f) -> what the CALLER asked for
    sender.send(...)                                  -> convert, then submit

A profile owns BOTH the conversion and the job configuration, because they are
one unit (see profiles.py). Adding a printer is a new profile class plus an entry
in PROFILES; function_app.py does not change, which is what keeps the route free
of per-printer branching.

TWO WAYS TO PICK A PROFILE, AND THEY ANSWER DIFFERENT QUESTIONS.

    matches(share, source)      "can this profile serve this printer?"
    produces(format, source)    "can this profile emit this format?"

`select_profile` walks `matches` and infers the format from the device's
capabilities -- the original behaviour, still the default when a caller expresses
no preference. `select_profile_for_format` walks `produces` and honours a format
the caller NAMED, which is what `printFormat` on the Submit endpoint sends.

The distinction is not cosmetic. PwgRasterProfile.matches deliberately stands
aside when the printer also accepts PDF, because rasterizing a document the
device could take directly is wasted work. A caller who explicitly asks for
`image/pwg-raster` has overruled that judgement, and only the `produces` path
lets them.

ADDING A FORMAT is a profile class with `matches`, `produces`,
`target_content_type`, `convert` and `job_configuration`, one entry in PROFILES,
and its MIME type in SUPPORTED_PRINT_FORMATS. Nothing else -- no route change, no
new branch in function_app.py. That is the whole point of the registry.

Order matters. PassthroughProfile is last so that a converting profile only gets
a chance when the printer genuinely cannot take the document as it stands.
"""

from __future__ import annotations

from typing import Any, Optional

from .profiles import (
    PDF,
    PWG_RASTER,
    PassthroughProfile,
    ProfileError,
    PwgRasterProfile,
    normalize_format,
)

PROFILES = (
    PwgRasterProfile(),
    PassthroughProfile(),
)

# The formats a caller may NAME in `printFormat`. Every entry must be producible
# by some profile in PROFILES -- otherwise the endpoint would advertise a format
# in its own 400 message that can never actually be selected. Asserted by
# test_print_format.py::test_every_advertised_format_can_actually_be_produced.
SUPPORTED_PRINT_FORMATS = (PDF, PWG_RASTER)


def select_profile(share: Any, source_content_type: str) -> Optional[Any]:
    """The first profile that can serve this printer, or None.

    None is not an error here: it means no conversion path exists, and the
    caller reports that with the same "does not accept ..." message the
    pipeline has always used.
    """
    for profile in PROFILES:
        if profile.matches(share, source_content_type):
            return profile
    return None


def select_profile_for_format(share: Any, source_content_type: str,
                              print_format: str) -> Optional[Any]:
    """The profile that emits `print_format` from this document, or None.

    The printer's capabilities are NOT consulted -- Submit has already refused
    the request at preflight if the device does not report the format, and doing
    it again here would let PwgRasterProfile.matches quietly veto an explicit
    choice. What still constrains the answer is the DOCUMENT: passthrough can
    only emit what the file already is, and the raster converter only reads PDF.

    None means this format cannot be produced from this document. That is a
    per-file failure, not a bad request: the format is fine, the file is wrong.
    """
    wanted = normalize_format(print_format)
    if not wanted:
        return None
    for profile in PROFILES:
        if profile.produces(wanted, source_content_type):
            return profile
    return None


def profile_for(share: Any, source_content_type: str,
                print_format: str = "") -> Optional[Any]:
    """Pick a profile the way the routes do: by name when one was given, by
    capability otherwise. One place so Submit, the dry run and the live test
    scripts cannot drift into three different answers."""
    if normalize_format(print_format):
        return select_profile_for_format(share, source_content_type, print_format)
    return select_profile(share, source_content_type)


__all__ = [
    "PDF",
    "PWG_RASTER",
    "PROFILES",
    "SUPPORTED_PRINT_FORMATS",
    "PassthroughProfile",
    "ProfileError",
    "PwgRasterProfile",
    "normalize_format",
    "profile_for",
    "select_profile",
    "select_profile_for_format",
]
