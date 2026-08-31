"""
make_test_pdf.py — emit a small, valid, single-page PDF for live printer tests.

Standard library only, no dependencies. Written by hand rather than pulled from a
PDF library because the point is a file we fully control: if the printer chokes,
the document is not the variable.

    .\\.venv\\Scripts\\python.exe scripts\\make_test_pdf.py out.pdf "line one" "line two"
"""

from __future__ import annotations

import datetime
import pathlib
import sys

# Where a bare invocation puts the file. samples/ is the project's home for real
# inputs and generated artifacts (playbook §2.1) and is gitignored, so a throwaway
# test page does not end up in the working tree -- which is where this used to
# land, in the repo ROOT, until one showed up there after a live run.
DEFAULT_DIR = pathlib.Path(__file__).resolve().parent.parent / "samples"
DEFAULT_NAME = "noble-print-test.pdf"


def build_pdf(lines) -> bytes:
    """A one-page Letter PDF containing `lines`, with a correct xref table.

    Offsets are computed as the objects are assembled -- a PDF with a wrong xref
    opens in some viewers and is rejected by others, which is exactly the
    ambiguity a printer test must not have.
    """
    text_ops = []
    y = 720
    for index, line in enumerate(lines):
        size = 18 if index == 0 else 12
        escaped = (str(line).replace("\\", r"\\")
                            .replace("(", r"\(")
                            .replace(")", r"\)"))
        text_ops.append("BT /F1 {} Tf 72 {} Td ({}) Tj ET".format(size, y, escaped))
        y -= 28 if index == 0 else 18
    stream = "\n".join(text_ops).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += "{} 0 obj\n".format(number).encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += "xref\n0 {}\n".format(len(objects) + 1).encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += "{:010d} 00000 n \n".format(offset).encode()
    out += ("trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF\n"
            .format(len(objects) + 1, xref_at).encode())
    return bytes(out)


def main() -> int:
    if len(sys.argv) > 1:
        path = pathlib.Path(sys.argv[1])
    else:
        # Resolved against the REPO, not the current directory, so the file lands
        # in the same place whichever folder the script is run from.
        path = DEFAULT_DIR / DEFAULT_NAME
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = sys.argv[2:] or [
        "noble-print live printer test",
        "Generated {}".format(stamp),
        "",
        "If this page came out of the tray, Universal Print delivered it",
        "end to end and the printer registration is working.",
    ]
    data = build_pdf(lines)
    with open(path, "wb") as handle:
        handle.write(data)
    print("wrote {} ({} bytes)".format(path, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
