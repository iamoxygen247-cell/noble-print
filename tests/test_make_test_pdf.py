"""
test_make_test_pdf.py — the generated test document must be a valid PDF.

The point of a hand-built test page is that the DOCUMENT is not a variable: if
the live printer check fails, it should be the printer or the registration, never
a malformed file. A PDF with a wrong xref opens in some viewers and is rejected
by others, which is exactly the ambiguity a printer test must not have -- so the
structure is verified rather than eyeballed.
"""

from __future__ import annotations

import pathlib
import re
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from make_test_pdf import build_pdf  # noqa: E402


def test_header_and_trailer():
    data = build_pdf(["hello"])
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")


def test_startxref_points_at_the_xref_table():
    """`startxref` must give the byte offset of the table itself.

    Worth its own test: searching the file for the word "xref" finds
    "startxref" first, and a checker that makes that mistake validates nothing
    while appearing to pass.
    """
    data = build_pdf(["hello"])
    offset = int(re.search(rb"startxref\s+(\d+)\s*%%EOF", data).group(1))
    assert data[offset:offset + 4] == b"xref"


def test_every_xref_offset_points_at_its_object():
    data = build_pdf(["one", "two"])
    start = int(re.search(rb"startxref\s+(\d+)\s*%%EOF", data).group(1))
    table = data[start:]

    declared = int(re.match(rb"xref\s+0 (\d+)", table).group(1))
    entries = re.findall(rb"(\d{10}) (\d{5}) ([nf])", table)

    assert len(entries) == declared, "the table declares a size it does not contain"
    assert entries[0][2] == b"f", "object 0 must be the free entry"

    for number, (offset, _gen, kind) in enumerate(entries[1:], start=1):
        assert kind == b"n"
        expected = "{} 0 obj".format(number).encode()
        assert data[int(offset):int(offset) + len(expected)] == expected


def test_contains_the_objects_a_one_page_document_needs():
    data = build_pdf(["hello"])
    for marker in (b"/Type /Catalog", b"/Type /Pages", b"/Type /Page ",
                   b"/Type /Font", b"stream", b"endstream"):
        assert marker in data, marker


def test_the_text_actually_appears_in_the_content_stream():
    data = build_pdf(["noble-print live test"])
    assert b"(noble-print live test) Tj" in data


def test_stream_length_matches_the_declared_length():
    """A wrong /Length is the classic hand-rolled-PDF bug: many viewers recover
    from it, and a printer's interpreter often will not."""
    data = build_pdf(["alpha", "beta", "gamma"])
    declared = int(re.search(rb"<< /Length (\d+) >>", data).group(1))
    body = re.search(rb"stream\n(.*?)\nendstream", data, re.S).group(1)
    assert len(body) == declared


def test_parentheses_and_backslashes_are_escaped():
    """Unescaped ( ) or \\ inside a PDF string terminates it early and corrupts
    the page. File names reach this text, and they do contain brackets."""
    data = build_pdf([r"Invoice (final) C:\\jobs"])
    stream = re.search(rb"stream\n(.*?)\nendstream", data, re.S).group(1)

    text = re.search(rb"\((.*)\) Tj", stream).group(1)
    assert rb"\(" in text and rb"\)" in text
    # The declared length must still be right after escaping.
    declared = int(re.search(rb"<< /Length (\d+) >>", data).group(1))
    assert len(stream) == declared


def test_empty_lines_are_tolerated():
    """The default document contains a blank spacer line."""
    data = build_pdf(["title", "", "footer"])
    assert data.startswith(b"%PDF")
    assert b"(title) Tj" in data and b"(footer) Tj" in data
