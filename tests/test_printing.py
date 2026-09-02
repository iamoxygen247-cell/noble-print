"""
test_printing.py — the per-printer conversion package.

Three tiers in one file, because they describe one feature:

  A  the PWG encoder            -- pure computation, no I/O
  B  profile selection and the job configuration it carries
  C  the Submit route with a raster-only printer, end to end

WHY THE BYTES ARE PINNED. functionapp/printing/pwg_converter.py was vendored from
a proof of concept whose correctness is evidenced by a page that physically came
out of a Brother MFC-L5800DW. Nothing in this repo can re-run that experiment, so
the next best guard is that the encoder still produces exactly what it produced
then: the conversion is byte-deterministic, verified across repeated runs, so a
hash pin turns "someone tidied the encoder" into a failing test rather than a
wasted ream of paper.

A pypdfium2 upgrade can legitimately change the rendered pixels and therefore the
hash. That is not a bug -- but it IS a reason to reprint one page and confirm the
device still likes it before accepting the new value.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys

import pytest

import print_policy
import printing
import universal_print
from printing import profiles
from printing import pwg_converter
from helpers import SITE, SUBMIT, as_json, post, result_for

BODY = {**SITE, "library": "Documents", "folder": "/Invoices/ToPrint",
        "printerShareId": "share-guid"}


def body(**overrides):
    merged = dict(BODY)
    merged.update(overrides)
    return merged


# --- a deterministic one-page US Letter PDF -----------------------------------
# scripts/make_test_pdf.py already builds one with the standard library, so the
# fixture needs no binary in the repo and no dependency of its own.

_FIXTURE_LINES = ["noble-print PWG raster fixture", "US Letter, deterministic"]

# Pinned from a run that also reproduced samples/invoice.pwg byte for byte.
FIXTURE_SHA256_300DPI = (
    "1fd9e6da4501543f42122d5e13415d6cf5a32d4e86b22825cd79d51804c3342c")
FIXTURE_BYTES_300DPI = 33897


def _make_test_pdf() -> bytes:
    script = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "make_test_pdf.py"
    spec = importlib.util.spec_from_file_location("_make_test_pdf", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_make_test_pdf"] = module
    spec.loader.exec_module(module)
    return module.build_pdf(_FIXTURE_LINES)


@pytest.fixture(scope="module")
def sample_pdf() -> bytes:
    return _make_test_pdf()


def raster_share(**overrides) -> universal_print.ShareInfo:
    """A printer that reports image/pwg-raster and nothing else, like the
    Brother MFC-L5800DW."""
    fields = dict(
        share_id="share-guid", printer_id="printer-guid",
        display_name="Raster Only", accepting_jobs=True,
        content_types=["image/pwg-raster"], state="idle",
        dpis=[300, 600], scalings=["fit", "none"],
        media_sizes=["North America Letter", "A4"],
        top_margins=[4320], bottom_margins=[4320],
        left_margins=[4320], right_margins=[4320],
    )
    fields.update(overrides)
    return universal_print.ShareInfo(**fields)


# --- Tier A: the encoder ------------------------------------------------------


def test_the_encoder_produces_the_documented_pwg_shape(sample_pdf):
    """Every field here was read out of the file that printed."""
    data = pwg_converter.convert_pdf(sample_pdf, dpi=300)

    assert data[:4] == b"RaS2"
    page = pwg_converter.validate_pwg(data)[0]
    assert (page.width, page.height) == (2550, 3300), "US Letter at 300 dpi"
    assert (page.dpi_x, page.dpi_y) == (300, 300)
    assert (page.page_width_points, page.page_height_points) == (612, 792)
    assert page.page_size_name == "na_letter_8.5x11in"


def test_the_encoder_round_trips_through_its_own_validator(sample_pdf):
    """validate_pwg decodes independently of the encoder, so agreement between
    them is a real check rather than a restatement."""
    data = pwg_converter.convert_pdf(sample_pdf, dpi=72)

    pages = pwg_converter.validate_pwg(data)
    assert len(pages) == 1
    assert (pages[0].width, pages[0].height) == (612, 792)
    assert pages[0].minimum_gray < pages[0].maximum_gray, "the page has ink on it"


def test_the_conversion_is_byte_identical_to_the_pinned_output(sample_pdf):
    """See the module docstring: this is the closest thing the offline suite has
    to the physical print that validated this encoder."""
    data = pwg_converter.convert_pdf(sample_pdf, dpi=300)

    assert len(data) == FIXTURE_BYTES_300DPI
    assert hashlib.sha256(data).hexdigest() == FIXTURE_SHA256_300DPI, (
        "the encoder's output changed. If this followed a pypdfium2 upgrade the "
        "new bytes may be fine -- but print one page and confirm the device "
        "accepts it before updating this hash.")


def test_an_empty_or_non_pdf_input_is_refused():
    with pytest.raises(pwg_converter.ConversionError):
        pwg_converter.convert_pdf(b"")
    with pytest.raises(pwg_converter.ConversionError):
        pwg_converter.convert_pdf(b"this is not a PDF")


# --- Tier B: profile selection ------------------------------------------------


def test_a_raster_only_printer_selects_the_converting_profile():
    profile = printing.select_profile(raster_share(), "application/pdf")

    assert profile is not None
    assert profile.name == "pdf-to-pwg-raster"
    assert profile.target_content_type("application/pdf") == "image/pwg-raster"


def test_a_pdf_printer_selects_passthrough_and_changes_nothing():
    share = raster_share(content_types=["application/pdf", "application/oxps"])

    profile = printing.select_profile(share, "application/pdf")

    assert profile.name == "passthrough"
    assert profile.convert(b"%PDF-1.7 original", share) == b"%PDF-1.7 original"
    assert profile.target_content_type("application/pdf") == "application/pdf"
    assert profile.job_configuration(share) == print_policy.JOB_CONFIGURATION


def test_passthrough_wins_when_the_printer_accepts_both():
    """Converting a document the printer would have taken as-is would cost a
    rasterize and a much larger upload for nothing."""
    share = raster_share(content_types=["application/pdf", "image/pwg-raster"])

    assert printing.select_profile(share, "application/pdf").name == "passthrough"


def test_a_printer_that_accepts_neither_selects_no_profile():
    share = raster_share(content_types=["application/oxps"])

    assert printing.select_profile(share, "application/pdf") is None


def test_a_printer_reporting_nothing_is_given_the_benefit_of_the_doubt():
    """ShareInfo.supports() treats an empty capability list as unknown rather
    than as a refusal, and profile selection must not undo that."""
    share = raster_share(content_types=[])

    assert printing.select_profile(share, "application/pdf").name == "passthrough"


# --- Tier B: the job configuration the raster profile carries -----------------


def test_the_raster_configuration_only_uses_values_the_printer_reports():
    share = raster_share()

    config = printing.PwgRasterProfile().job_configuration(share)

    assert config["dpi"] in share.dpis
    assert config["scaling"] in share.scalings
    assert config["mediaSize"] in share.media_sizes


def test_the_raster_configuration_keeps_fit_scaling_and_margins_together():
    """These two are load-bearing, not cosmetic. pwg_converter renders a
    full-bleed page the exact size of the media and deliberately does not inset
    the unprintable margins; `fit` plus the device margins is what places it on
    the sheet. Drop either and the page prints cropped."""
    config = printing.PwgRasterProfile().job_configuration(raster_share())

    assert config["scaling"] == "fit"
    assert config["margin"] == {"top": 4320, "bottom": 4320,
                                "left": 4320, "right": 4320}


def test_margins_fall_back_when_the_printer_reports_none():
    share = raster_share(top_margins=[], bottom_margins=[],
                         left_margins=[], right_margins=[])

    config = printing.PwgRasterProfile().job_configuration(share)

    assert config["margin"]["top"] == profiles.FALLBACK_MARGIN_MICRONS


def test_the_dpi_drops_to_something_the_printer_offers():
    """A printer that cannot do 300 should get the best it can, not a failure."""
    share = raster_share(dpis=[150, 200])

    assert printing.PwgRasterProfile().dpi(share) == 200


def test_the_requested_dpi_is_used_when_the_printer_reports_none():
    assert printing.PwgRasterProfile().dpi(raster_share(dpis=[])) == 300


def test_an_out_of_range_dpi_setting_warns_and_falls_back(monkeypatch):
    """Server misconfiguration must not take the run down -- the same rule
    print_policy._resolve_int follows for every other tunable."""
    monkeypatch.setenv("PRINT_RASTER_DPI", "99999")

    assert printing.PwgRasterProfile().dpi(raster_share()) == 300


def test_an_oversized_raster_is_refused_with_a_readable_reason(sample_pdf,
                                                              monkeypatch):
    monkeypatch.setenv("PRINT_RASTER_MAX_BYTES", "2048")

    with pytest.raises(profiles.ProfileError) as excinfo:
        printing.PwgRasterProfile().convert(sample_pdf, raster_share())

    assert "PRINT_RASTER_DPI" in str(excinfo.value)


# --- Tier C: the Submit route with a raster-only printer ----------------------


@pytest.fixture
def raster_graph(graph, sample_pdf):
    """FakeGraph reporting a PWG-raster-only printer, serving a real PDF."""
    graph.use_capabilities(
        contentTypes=["image/pwg-raster"],
        dpis=[300, 600],
        scalings=["fit", "none"],
        mediaSizes=["North America Letter"],
        topMargins=[4320], bottomMargins=[4320],
        leftMargins=[4320], rightMargins=[4320],
    )
    graph.anon.content = sample_pdf
    return graph


def test_submit_uploads_pwg_raster_to_a_raster_only_printer(raster_graph):
    raster_graph.add_item("1", name="invoice.pdf")

    payload = as_json(post(SUBMIT, body()))

    assert payload["submitted"] == 1
    assert raster_graph.status_of("1") == print_policy.PENDING

    session = raster_graph.calls_to("/createUploadSession", method="POST")[0]
    assert session.body["properties"]["contentType"] == "image/pwg-raster"

    uploaded = b"".join(raster_graph.anon.uploaded.values())
    assert uploaded[:4] == b"RaS2", "the raster was uploaded, not the PDF"
    assert session.body["properties"]["size"] == len(uploaded)


def test_the_job_configuration_travels_with_the_raster(raster_graph):
    raster_graph.add_item("1", name="invoice.pdf")

    post(SUBMIT, body())

    created = raster_graph.created_jobs()[0].body["configuration"]
    assert created["scaling"] == "fit"
    assert created["dpi"] == 300
    assert created["margin"]["top"] == 4320
    assert created["colorMode"] == "grayscale"


def test_a_conversion_failure_is_reported_against_the_document(raster_graph):
    """Not as a printer fault: the stage prefix is what tells whoever reads
    Print_Message which system to go and look at."""
    raster_graph.anon.content = b"%PDF-1.7 truncated nonsense"
    raster_graph.add_item("1", name="invoice.pdf")

    payload = as_json(post(SUBMIT, body()))

    assert payload["failed"] == 1
    assert raster_graph.status_of("1") == print_policy.FAILED
    assert raster_graph.field("1", "Print_Message").startswith("convert:")
    assert raster_graph.created_jobs() == [], "no job for a document we cannot send"


def test_an_unconvertible_printer_still_reports_the_old_refusal(graph):
    """The message two other tests already assert on must survive the profile
    layer, because it is what a human reads in the column."""
    graph.use_capabilities(contentTypes=["application/oxps"])
    graph.add_item("1", name="invoice.pdf")

    payload = as_json(post(SUBMIT, body()))

    assert payload["failed"] == 1
    message = graph.field("1", "Print_Message")
    assert "does not accept" in message
    assert "application/pdf" in message
    assert graph.created_jobs() == []


def test_a_pdf_printer_still_uploads_the_original_bytes(graph):
    """The passthrough path must be byte-for-byte what it was before profiles
    existed -- no rasterizing, no configuration change."""
    graph.anon.content = b"%PDF-1.7 original document"
    graph.add_item("1", name="invoice.pdf")

    post(SUBMIT, body())

    session = graph.calls_to("/createUploadSession", method="POST")[0]
    assert session.body["properties"]["contentType"] == "application/pdf"
    assert b"".join(graph.anon.uploaded.values()) == b"%PDF-1.7 original document"
    assert graph.created_jobs()[0].body == {"configuration": {"copies": 1}}
