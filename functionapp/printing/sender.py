"""
sender.py — hand a prepared document to Universal Print.

Deliberately thin. Every Graph call still goes through universal_print.py, which
owns the four-step submit protocol and the two rules that must not be re-derived
here: the upload PUT carries no Authorization header, and cancel addresses the
printer id rather than the share id. Re-implementing any of that would give the
repo two versions of a sequence that 487 tests currently pin to one.

What this module adds is the part that is per-printer: convert with the profile,
then submit with the profile's job configuration.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import universal_print

STAGE_CONVERT = "convert"


def prepare(profile: Any, share: Any, data: bytes,
            source_content_type: str) -> Tuple[bytes, str, Dict[str, Any]]:
    """Run the profile's conversion. Returns (bytes, content type, job config).

    Separated from send() so a caller can convert without submitting -- the dry
    run reports what would be produced, and the tests check the bytes without
    reaching the transport.
    """
    payload = profile.convert(data, share)
    content_type = profile.target_content_type(source_content_type)
    configuration = profile.job_configuration(share)
    return payload, content_type, configuration


def send(client: Any, share: Any, profile: Any, data: bytes, file_name: str,
         source_content_type: str, timeout: Optional[float] = None) -> str:
    """Convert and submit. Returns the print job id.

    A conversion failure is raised as PrintStageError("convert", ...) so it
    lands in Print_Message with the same stage prefix every other submission
    failure uses, and points at the document rather than at the printer.
    """
    try:
        payload, content_type, configuration = prepare(
            profile, share, data, source_content_type)
    except Exception as exc:
        raise universal_print.PrintStageError(STAGE_CONVERT, exc)

    return universal_print.submit_document(
        client, share.share_id, payload, file_name, content_type,
        configuration=configuration, timeout=timeout)
