#!/usr/bin/env python3
"""Convert a PDF file to an 8-bit grayscale PWG Raster file.

This converter uses pypdfium2/PDFium to render PDF pages and a small,
standalone PWG 5102.4 encoder to produce ``image/pwg-raster`` data. It does
not use Ghostscript, connect to Microsoft Graph, or submit a print job.

Install the dependency:

    python -m pip install "pypdfium2>=5,<6"

Example:

    python create-pwg-from-pdf.py invoice.pdf invoice.pwg --dpi 300

Each PDF page is normalized to the exact pixel dimensions implied by its
media size and the requested resolution. For example, US Letter at 300 DPI
is always encoded as 2550 by 3300 pixels. Printer margins are intentionally
not baked into this file; the companion Universal Print script uses
``scaling = fit`` and the printer's reported margins.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import pathlib
import struct
import sys
import tempfile
from dataclasses import dataclass


PWG_MAGIC = b"RaS2"
PWG_PAGE_HEADER_SIZE = 1796
PWG_CONTENT_TYPE = "image/pwg-raster"

DEFAULT_DPI = 300
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_PAGES = 100
MAX_PIXELS_PER_PAGE = 50_000_000
MAX_OUTPUT_BYTES = 256 * 1024 * 1024


class ConversionError(RuntimeError):
    """The PDF could not be converted to a valid PWG Raster stream."""


@dataclass(frozen=True)
class PageInfo:
    width: int
    height: int
    dpi_x: int
    dpi_y: int
    page_width_points: int
    page_height_points: int
    page_size_name: str
    minimum_gray: int
    maximum_gray: int


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _round_positive(value: float) -> int:
    """Round a positive value to the nearest integer, with halves upward."""
    if not math.isfinite(value) or value <= 0:
        raise ConversionError(f"invalid positive measurement: {value!r}")
    return max(1, int(math.floor(value + 0.5)))


def _pixels_from_points(points: float, dpi: int) -> int:
    return _round_positive(points * dpi / 72.0)


def _page_size_name(width_points: float, height_points: float) -> str:
    """Return a PWG standardized media keyword for common page sizes."""
    sizes = (
        ("na_letter_8.5x11in", 612.0, 792.0),
        ("na_legal_8.5x14in", 612.0, 1008.0),
        ("na_tabloid_11x17in", 792.0, 1224.0),
        ("iso_a4_210x297mm", 595.2755906, 841.8897638),
    )
    tolerance_points = 0.5
    for name, expected_width, expected_height in sizes:
        if (
            abs(width_points - expected_width) <= tolerance_points
            and abs(height_points - expected_height) <= tolerance_points
        ):
            return name
    return ""


def _put_cstring(header: bytearray, offset: int, value: str) -> None:
    encoded = value.encode("ascii")
    if len(encoded) > 63:
        raise ConversionError(f"PWG header string is too long: {value!r}")
    header[offset : offset + len(encoded)] = encoded


def _get_cstring(header: bytes, offset: int) -> str:
    value = header[offset : offset + 64].split(b"\0", 1)[0]
    try:
        return value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ConversionError("PWG header contains a non-ASCII string") from exc


def _page_header(
    width: int,
    height: int,
    dpi: int,
    page_count: int,
    *,
    page_width_points: float,
    page_height_points: float,
    page_size_name: str,
) -> bytes:
    """Create a network-byte-order PWG page header for type sgray_8."""
    if width <= 0 or height <= 0:
        raise ConversionError(f"invalid raster size {width}x{height}")

    header = bytearray(PWG_PAGE_HEADER_SIZE)
    _put_cstring(header, 0, "PwgRaster")
    _put_cstring(header, 1732, page_size_name)

    def put_uint(offset: int, value: int) -> None:
        if not 0 <= value <= 0xFFFFFFFF:
            raise ConversionError(f"PWG unsigned integer is out of range: {value}")
        struct.pack_into(">I", header, offset, value)

    def put_sint(offset: int, value: int) -> None:
        struct.pack_into(">i", header, offset, value)

    put_uint(276, dpi)  # HWResolution cross-feed
    put_uint(280, dpi)  # HWResolution feed
    put_uint(352, _round_positive(page_width_points))
    put_uint(356, _round_positive(page_height_points))
    put_uint(372, width)
    put_uint(376, height)
    put_uint(384, 8)  # BitsPerColor
    put_uint(388, 8)  # BitsPerPixel
    put_uint(392, width)  # BytesPerLine
    put_uint(396, 0)  # Chunky color order
    put_uint(400, 18)  # sGray
    put_uint(420, 1)  # NumColors
    put_uint(452, page_count)
    put_sint(456, 1)  # CrossFeedTransform
    put_sint(460, 1)  # FeedTransform
    return bytes(header)


def _pack_line(line: bytes) -> bytes:
    """Encode one 8-bit grayscale row using PWG's PackBits-like format."""
    output = bytearray()
    position = 0
    width = len(line)

    while position < width:
        distance = 1
        while (
            distance < 128
            and position + distance < width
            and line[position + distance - 1] != line[position + distance]
        ):
            distance += 1

        if distance == 1:
            repeated = 1
            while (
                repeated < 128
                and position + repeated < width
                and line[position] == line[position + repeated]
            ):
                repeated += 1
            output.append(repeated - 1)
            output.append(line[position])
            position += repeated
        else:
            output.append(257 - distance)
            output.extend(line[position : position + distance])
            position += distance

    return bytes(output)


def _write_page(
    output: io.BytesIO,
    pixels: memoryview,
    *,
    width: int,
    height: int,
    stride: int,
    dpi: int,
    page_count: int,
    page_width_points: float,
    page_height_points: float,
    page_size_name: str,
) -> None:
    output.write(
        _page_header(
            width,
            height,
            dpi,
            page_count,
            page_width_points=page_width_points,
            page_height_points=page_height_points,
            page_size_name=page_size_name,
        )
    )

    row_number = 0
    while row_number < height:
        start = row_number * stride
        row = bytes(pixels[start : start + width])

        repeated_rows = 1
        while repeated_rows < 256 and row_number + repeated_rows < height:
            other_start = (row_number + repeated_rows) * stride
            if bytes(pixels[other_start : other_start + width]) != row:
                break
            repeated_rows += 1

        output.write(bytes((repeated_rows - 1,)))
        output.write(_pack_line(row))
        if output.tell() > MAX_OUTPUT_BYTES:
            raise ConversionError(
                f"PWG output exceeds {MAX_OUTPUT_BYTES:,} bytes"
            )
        row_number += repeated_rows


def _render_exact_page(page, *, dpi: int, page_number: int):
    """Render a page and center it on its exact media-sized white canvas."""
    page_width_points, page_height_points = page.get_size()
    if (
        not math.isfinite(page_width_points)
        or not math.isfinite(page_height_points)
        or page_width_points <= 0
        or page_height_points <= 0
    ):
        raise ConversionError(
            f"page {page_number} has invalid size "
            f"{page_width_points!r}x{page_height_points!r} points"
        )

    canvas_width = _pixels_from_points(page_width_points, dpi)
    canvas_height = _pixels_from_points(page_height_points, dpi)
    canvas_pixels = canvas_width * canvas_height
    if canvas_pixels > MAX_PIXELS_PER_PAGE:
        raise ConversionError(
            f"page {page_number} requires {canvas_width}x{canvas_height} "
            f"pixels; limit is {MAX_PIXELS_PER_PAGE:,} pixels"
        )

    # PDFium rounds rendered extents upward. Choosing the next representable
    # scale toward zero prevents 792 pt * (300/72) from becoming 3301 pixels.
    scale = min(
        canvas_width / page_width_points,
        canvas_height / page_height_points,
    )
    scale = math.nextafter(scale, 0.0)

    bitmap = None
    source = None
    try:
        # The small retry loop protects against platform-specific PDFium
        # rounding while never cropping rendered content.
        for _ in range(4):
            bitmap = page.render(
                scale=scale,
                grayscale=True,
                may_draw_forms=True,
                fill_color=(255, 255, 255, 255),
                optimize_mode="print",
            )
            rendered_width = int(bitmap.width)
            rendered_height = int(bitmap.height)
            if (
                rendered_width <= canvas_width
                and rendered_height <= canvas_height
            ):
                break

            shrink = min(
                canvas_width / rendered_width,
                canvas_height / rendered_height,
            )
            bitmap.close()
            bitmap = None
            scale = math.nextafter(scale * shrink, 0.0)
        else:
            raise ConversionError(
                f"page {page_number} could not be rendered within its "
                f"{canvas_width}x{canvas_height} media canvas"
            )

        rendered_width = int(bitmap.width)
        rendered_height = int(bitmap.height)
        rendered_stride = int(bitmap.stride)
        if rendered_stride < rendered_width:
            raise ConversionError(
                f"page {page_number} has invalid rendered stride "
                f"{rendered_stride}"
            )

        source = memoryview(bitmap.buffer).cast("B")
        if len(source) < rendered_stride * rendered_height:
            raise ConversionError(
                f"page {page_number} has a truncated rendered bitmap"
            )

        canvas = bytearray([255]) * canvas_pixels
        offset_x = (canvas_width - rendered_width) // 2
        offset_y = (canvas_height - rendered_height) // 2

        for row_number in range(rendered_height):
            source_start = row_number * rendered_stride
            source_end = source_start + rendered_width
            canvas_start = (
                (offset_y + row_number) * canvas_width + offset_x
            )
            canvas[canvas_start : canvas_start + rendered_width] = source[
                source_start:source_end
            ]

        return (
            canvas,
            canvas_width,
            canvas_height,
            page_width_points,
            page_height_points,
            _page_size_name(page_width_points, page_height_points),
        )
    finally:
        if source is not None:
            source.release()
        if bitmap is not None:
            bitmap.close()


def convert_pdf(pdf_data: bytes, dpi: int = DEFAULT_DPI) -> bytes:
    """Render PDF bytes and return a complete PWG Raster document."""
    if not pdf_data:
        raise ConversionError("input PDF is empty")
    if len(pdf_data) > MAX_INPUT_BYTES:
        raise ConversionError(
            f"input PDF exceeds {MAX_INPUT_BYTES:,} bytes"
        )
    if b"%PDF-" not in pdf_data[:1024]:
        raise ConversionError("input does not contain a PDF header")

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise ConversionError(
            'pypdfium2 is required; install it with: '
            'python -m pip install "pypdfium2>=5,<6"'
        ) from exc

    document = None
    output = io.BytesIO()
    output.write(PWG_MAGIC)

    try:
        document = pdfium.PdfDocument(pdf_data)
        # Form initialization must happen before getting page handles or the
        # document length. It is a no-op for PDFs without forms.
        document.init_forms()

        page_count = len(document)
        if page_count == 0:
            raise ConversionError("PDF contains no pages")
        if page_count > MAX_PAGES:
            raise ConversionError(
                f"PDF has {page_count} pages; limit is {MAX_PAGES}"
            )

        for page_index in range(page_count):
            page = document[page_index]
            pixels = None
            try:
                (
                    canvas,
                    width,
                    height,
                    page_width_points,
                    page_height_points,
                    page_size_name,
                ) = _render_exact_page(
                    page,
                    dpi=dpi,
                    page_number=page_index + 1,
                )

                pixels = memoryview(canvas)
                _write_page(
                    output,
                    pixels,
                    width=width,
                    height=height,
                    stride=width,
                    dpi=dpi,
                    page_count=page_count,
                    page_width_points=page_width_points,
                    page_height_points=page_height_points,
                    page_size_name=page_size_name,
                )
            finally:
                if pixels is not None:
                    pixels.release()
                page.close()
    except ConversionError:
        raise
    except Exception as exc:
        raise ConversionError(f"PDF rendering failed: {exc}") from exc
    finally:
        if document is not None:
            document.close()

    return output.getvalue()


def _uint(header: bytes, offset: int) -> int:
    return struct.unpack_from(">I", header, offset)[0]


def validate_pwg(data: bytes) -> list[PageInfo]:
    """Independently decode every page and verify the generated PWG stream."""
    if not data.startswith(PWG_MAGIC):
        raise ConversionError("output is missing the PWG RaS2 magic")

    position = len(PWG_MAGIC)
    pages: list[PageInfo] = []
    expected_pages: int | None = None

    while position < len(data):
        header_end = position + PWG_PAGE_HEADER_SIZE
        if header_end > len(data):
            raise ConversionError("output contains a truncated page header")
        header = data[position:header_end]
        position = header_end

        width = _uint(header, 372)
        height = _uint(header, 376)
        dpi_x = _uint(header, 276)
        dpi_y = _uint(header, 280)
        page_width_points = _uint(header, 352)
        page_height_points = _uint(header, 356)
        page_count = _uint(header, 452)
        page_size_name = _get_cstring(header, 1732)

        if header[:64].rstrip(b"\0") != b"PwgRaster":
            raise ConversionError("page header does not identify PwgRaster")
        if not width or not height:
            raise ConversionError("page header contains an invalid size")
        if width * height > MAX_PIXELS_PER_PAGE:
            raise ConversionError("page header exceeds the configured pixel limit")
        if dpi_x == 0 or dpi_y == 0:
            raise ConversionError("page header contains an invalid resolution")
        if (
            _uint(header, 384) != 8
            or _uint(header, 388) != 8
            or _uint(header, 392) != width
            or _uint(header, 396) != 0
            or _uint(header, 400) != 18
            or _uint(header, 420) != 1
        ):
            raise ConversionError("page header is not valid sgray_8")

        if expected_pages is None:
            expected_pages = page_count
        elif page_count != expected_pages:
            raise ConversionError("page headers disagree about page count")

        rows_decoded = 0
        minimum_gray = 255
        maximum_gray = 0
        while rows_decoded < height:
            if position >= len(data):
                raise ConversionError("output is truncated before all rows")
            row_repetitions = data[position] + 1
            position += 1
            row_length = 0

            while row_length < width:
                if position >= len(data):
                    raise ConversionError("output is truncated inside a row")
                control = data[position]
                position += 1

                if control == 128:
                    raise ConversionError(
                        "output contains reserved PackBits control 0x80"
                    )
                if control <= 127:
                    count = control + 1
                    if position >= len(data):
                        raise ConversionError("output is truncated in a run")
                    value = data[position]
                    position += 1
                    minimum_gray = min(minimum_gray, value)
                    maximum_gray = max(maximum_gray, value)
                else:
                    count = 257 - control
                    end = position + count
                    if end > len(data):
                        raise ConversionError("output is truncated in a literal")
                    literal = data[position:end]
                    position = end
                    minimum_gray = min(minimum_gray, min(literal))
                    maximum_gray = max(maximum_gray, max(literal))

                row_length += count
                if row_length > width:
                    raise ConversionError("decoded row is wider than the header")

            rows_decoded += row_repetitions
            if rows_decoded > height:
                raise ConversionError("decoded page is taller than the header")

        pages.append(
            PageInfo(
                width=width,
                height=height,
                dpi_x=dpi_x,
                dpi_y=dpi_y,
                page_width_points=page_width_points,
                page_height_points=page_height_points,
                page_size_name=page_size_name,
                minimum_gray=minimum_gray,
                maximum_gray=maximum_gray,
            )
        )

        if expected_pages is not None and len(pages) == expected_pages:
            break

    if expected_pages is None or expected_pages == 0:
        raise ConversionError("output does not contain a page")
    if len(pages) != expected_pages:
        raise ConversionError(
            f"expected {expected_pages} pages, decoded {len(pages)}"
        )
    if position != len(data):
        raise ConversionError(f"output has {len(data) - position} trailing bytes")
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert PDF to validated 8-bit grayscale image/pwg-raster."
        )
    )
    parser.add_argument("input_pdf", type=pathlib.Path)
    parser.add_argument(
        "output_pwg",
        nargs="?",
        type=pathlib.Path,
        help="default: INPUT_PDF with a .pwg extension",
    )
    parser.add_argument("--dpi", type=_positive_int, default=DEFAULT_DPI)
    args = parser.parse_args(argv)

    input_path = args.input_pdf.expanduser().resolve()
    output_path = (
        args.output_pwg.expanduser().resolve()
        if args.output_pwg
        else input_path.with_suffix(".pwg")
    )

    if not input_path.is_file():
        parser.error(f"input file not found: {input_path}")
    if input_path == output_path:
        parser.error("input and output paths must be different")

    temporary_path: pathlib.Path | None = None
    try:
        input_size = input_path.stat().st_size
        if input_size > MAX_INPUT_BYTES:
            raise ConversionError(
                f"input PDF exceeds {MAX_INPUT_BYTES:,} bytes"
            )

        data = convert_pdf(input_path.read_bytes(), dpi=args.dpi)
        pages = validate_pwg(data)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to a sibling temporary file, validate the actual on-disk
        # bytes, and atomically replace the destination. A failed or
        # interrupted conversion therefore cannot leave a partial PWG file.
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = pathlib.Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        disk_data = temporary_path.read_bytes()
        pages = validate_pwg(disk_data)
        os.replace(temporary_path, output_path)
        temporary_path = None
        data = disk_data
    except (OSError, ConversionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    report = {
        "status": "ok",
        "contentType": PWG_CONTENT_TYPE,
        "magic": PWG_MAGIC.decode("ascii"),
        "input": str(input_path),
        "output": str(output_path),
        "outputBytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "pageCount": len(pages),
        "pages": [
            {
                "page": index,
                "width": page.width,
                "height": page.height,
                "dpi": [page.dpi_x, page.dpi_y],
                "pageSizePoints": [
                    page.page_width_points,
                    page.page_height_points,
                ],
                "pageSizeName": page.page_size_name,
                "grayRange": [page.minimum_gray, page.maximum_gray],
            }
            for index, page in enumerate(pages, start=1)
        ],
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
