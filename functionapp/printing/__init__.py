"""
printing — pluggable per-printer document preparation.

The pipeline pulls a PDF out of SharePoint and hands it to Universal Print. Some
printers take that PDF as-is; others accept only a raster format. Universal Print
will not convert for us -- Microsoft documents exactly one conversion, OXPS to
PDF, and only for printers that already accept PDF -- so the client must produce
whatever the device reports.

This package keeps that per-device knowledge in one place:

    select_profile(share, content_type)  ->  a profile, or None
    sender.send(...)                     ->  convert, then submit

A profile owns BOTH the conversion and the job configuration, because they are
one unit (see profiles.py). Adding a printer is a new profile class plus an entry
in PROFILES; function_app.py does not change, which is what keeps the route free
of per-printer branching.

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
)

PROFILES = (
    PwgRasterProfile(),
    PassthroughProfile(),
)


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


__all__ = [
    "PDF",
    "PWG_RASTER",
    "PROFILES",
    "PassthroughProfile",
    "ProfileError",
    "PwgRasterProfile",
    "select_profile",
]
