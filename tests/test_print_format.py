"""
test_print_format.py — the caller naming its own upload format.

Submit chose a profile from the PRINTER'S CAPABILITIES and nothing else. That is
still the default, but a caller can now name `printFormat` and get the profile
that produces it, whatever the device happens to report.

The distinction the tests below exist to pin:

    matches(share, source)     "can this profile serve this printer?"
    produces(format, source)   "can this profile emit this format?"

PwgRasterProfile.matches deliberately STANDS ASIDE when the printer also accepts
PDF, because rasterizing a document the device could take directly is wasted
work. So on a dual-capability printer the two selectors give different answers,
and that gap is exactly what `printFormat` exists to cross. A test that only ever
used a raster-only printer would pass with the parameter wired to nothing.

Two refusals, deliberately different in kind:

    the PRINTER does not report the format   -> 400, at preflight, nothing claimed
    the DOCUMENT cannot produce it           -> per-file PRINT_FAILED

The first is the flow's configuration and will never fix itself; the second is
one bad file among good ones and must not take the batch down with it.
"""

from __future__ import annotations

import pytest

import print_policy
import printing
import universal_print
from helpers import SUBMIT, as_json, post, result_for

BODY = {"library": "Documents", "folder": "/Invoices/ToPrint",
        "printerShareId": "share-guid"}

PDF = "application/pdf"
PWG = "image/pwg-raster"


def body(**overrides):
    merged = dict(BODY)
    merged.update(overrides)
    return merged


def uploaded_content_type(graph) -> str:
    """The contentType declared on the upload session -- what Universal Print is
    told the bytes are."""
    session = graph.calls_to("/createUploadSession", method="POST")[0]
    return session.body["properties"]["contentType"]


def uploaded_bytes(graph) -> bytes:
    return b"".join(graph.anon.uploaded.values())


def dual_capability(graph):
    """A printer that accepts BOTH formats.

    The interesting fixture, and the one the default FakeGraph share already is:
    capability-based selection picks passthrough here, so any test that sees
    pwg-raster come out proves the REQUEST drove the choice.
    """
    graph.use_capabilities(contentTypes=[PDF, PWG], dpis=[300],
                           scalings=["fit"], mediaSizes=["North America Letter"])


# --- the registry ------------------------------------------------------------


def test_every_advertised_format_can_actually_be_produced():
    """SUPPORTED_PRINT_FORMATS is what the 400 message offers the caller. An
    entry no profile can emit would be advertised and then rejected per-file,
    which is a worse failure than never offering it."""
    for fmt in printing.SUPPORTED_PRINT_FORMATS:
        assert any(p.produces(fmt, PDF) for p in printing.PROFILES), \
            "{} is advertised but no profile produces it from a PDF".format(fmt)


def test_pwg_is_selectable_even_when_the_printer_also_takes_pdf():
    """The gap between `matches` and `produces`, stated directly. This is the
    whole reason the parameter exists: capability selection would pick
    passthrough here, because rasterizing what the device accepts natively is
    wasted work -- until the caller says otherwise."""
    share = universal_print.ShareInfo(
        share_id="s", printer_id="p", display_name="Dual", accepting_jobs=True,
        content_types=[PDF, PWG])

    assert printing.select_profile(share, PDF).name == "passthrough"
    assert printing.select_profile_for_format(share, PDF, PWG).name == \
        "pdf-to-pwg-raster"


def test_passthrough_only_produces_the_format_the_document_already_is():
    """It uploads the bytes untouched, so claiming any other format would be a
    lie that the printer discovers after the upload."""
    profile = printing.PassthroughProfile()

    assert profile.produces(PDF, PDF)
    assert not profile.produces(PWG, PDF)
    assert not profile.produces(PDF, "application/vnd.ms-word")


def test_the_raster_converter_refuses_a_non_pdf_source():
    """pwg_converter renders PDF pages and nothing else."""
    profile = printing.PwgRasterProfile()

    assert profile.produces(PWG, PDF)
    assert not profile.produces(PWG, "image/jpeg")


@pytest.mark.parametrize("supplied", [
    "APPLICATION/PDF",
    "application/pdf; charset=binary",
    "  application/pdf  ",
])
def test_format_matching_ignores_case_and_parameters(supplied):
    """Capability lists, request bodies and drive-item content types all arrive
    in different shapes; one normaliser keeps them comparable."""
    assert printing.normalize_format(supplied) == PDF


# --- request validation ------------------------------------------------------


def test_an_unknown_format_is_a_400_and_claims_nothing(graph):
    """A typo must not fall back to guessing -- guessing is the thing the
    parameter was added to stop."""
    graph.add_item("1", status=print_policy.READY)

    response = post(SUBMIT, body(printFormat="image/png"))

    assert response.status_code == 400
    assert "not supported" in as_json(response)["error"]
    assert graph.created_jobs() == []
    assert graph.items["1"]["fields"][graph.columns["Print_Status"]] == \
        print_policy.READY


def test_a_format_the_printer_does_not_report_is_a_400(graph):
    """Refused at PREFLIGHT, while the queue is untouched. Checked ahead of the
    accepting-jobs branch on purpose: a misconfigured flow does not fix itself
    when the printer comes back, so answering 'printer offline' would hide it."""
    graph.use_capabilities(contentTypes=[PDF])
    graph.add_item("1", status=print_policy.READY)

    response = post(SUBMIT, body(printFormat=PWG))

    assert response.status_code == 400
    error = as_json(response)["error"]
    assert PWG in error and "does not accept" in error
    assert graph.created_jobs() == []
    assert graph.items["1"]["fields"][graph.columns["Print_Status"]] == \
        print_policy.READY


def test_a_printer_reporting_no_content_types_is_given_the_benefit_of_the_doubt(graph):
    """ShareInfo.supports treats an empty list as 'unknown', not 'refuses'.
    Blocking a print because a device under-reports would be worse than letting
    the upload fail with a real error, and printFormat must not change that."""
    graph.use_capabilities(dpis=[300], scalings=["fit"],
                           mediaSizes=["North America Letter"])
    graph.add_item("1", status=print_policy.READY)

    response = post(SUBMIT, body(printFormat=PWG))

    assert response.status_code == 200


@pytest.mark.parametrize("supplied", [123, [], {}, True])
def test_a_non_string_format_is_a_400(graph, supplied):
    graph.add_item("1", status=print_policy.READY)

    response = post(SUBMIT, body(printFormat=supplied))

    assert response.status_code == 400


@pytest.mark.parametrize("supplied", [None, "", "   "])
def test_an_absent_or_blank_format_falls_back_to_the_capabilities(graph, supplied):
    """The pre-parameter behaviour, which every existing flow relies on."""
    dual_capability(graph)
    graph.add_item("1", status=print_policy.READY)

    payload = as_json(post(SUBMIT, body(printFormat=supplied)))

    assert payload["printFormat"] is None
    assert result_for(payload, "1")["result"] == "submitted"
    # Capability selection prefers passthrough on a printer that takes PDF.
    assert uploaded_content_type(graph) == PDF


# --- the format actually reaches the wire ------------------------------------


def test_requesting_pdf_uploads_the_document_unconverted(graph):
    """"application/pdf" needs no conversion because the invoice already is
    one -- so the bytes on the wire must be the bytes from SharePoint."""
    dual_capability(graph)
    graph.add_item("1", status=print_policy.READY)

    payload = as_json(post(SUBMIT, body(printFormat=PDF)))

    assert payload["printFormat"] == PDF
    assert result_for(payload, "1")["result"] == "submitted"
    assert uploaded_content_type(graph) == PDF
    assert uploaded_bytes(graph) == graph.anon.content


def test_requesting_pwg_raster_runs_the_conversion(graph, letter_pdf):
    """The other half: same printer, same file, different requested format, and
    now the raster converter runs. Nothing about the printer changed between
    this test and the one above -- only the request did."""
    dual_capability(graph)
    graph.anon.content = letter_pdf
    graph.add_item("1", status=print_policy.READY)

    payload = as_json(post(SUBMIT, body(printFormat=PWG)))

    assert payload["printFormat"] == PWG
    assert result_for(payload, "1")["result"] == "submitted"
    assert uploaded_content_type(graph) == PWG
    assert uploaded_bytes(graph) != letter_pdf, "the PDF was uploaded unconverted"
    assert uploaded_bytes(graph)[:4] == b"RaS2", "not a PWG raster stream"


def test_the_job_configuration_follows_the_requested_format(graph, letter_pdf):
    """The raster and its job configuration are a matched pair: the converter
    renders full-bleed at the media size and relies on `scaling: fit` plus the
    device margins to place it. Choosing the profile by request must carry the
    configuration too, or the page prints cropped."""
    dual_capability(graph)
    graph.anon.content = letter_pdf
    graph.add_item("1", status=print_policy.READY)

    post(SUBMIT, body(printFormat=PWG))

    configuration = graph.created_jobs()[0].body["configuration"]
    assert configuration["scaling"] == "fit"
    assert configuration["dpi"] == 300
    assert "margin" in configuration


# --- per-file refusal, not a bad request -------------------------------------


def test_a_document_the_format_cannot_be_produced_from_fails_only_that_file(
        graph, letter_pdf):
    """The request is fine and the printer is fine; this one FILE is wrong. It
    must land on the row as PRINT_FAILED and leave its neighbours alone."""
    dual_capability(graph)
    graph.anon.content = letter_pdf
    graph.add_item("1", status=print_policy.READY, name="scan.png",
                   mime_type="image/png")
    graph.add_item("2", status=print_policy.READY, name="invoice.pdf")

    payload = as_json(post(SUBMIT, body(printFormat=PWG)))

    assert len(payload["items"]) == 2, "the bad file aborted the batch"
    bad = result_for(payload, "1")
    assert bad["result"] == "failed"
    assert "cannot produce" in bad["message"]
    assert graph.items["1"]["fields"][graph.columns["Print_Status"]] == \
        print_policy.FAILED
    assert result_for(payload, "2")["result"] == "submitted"


# --- the dry run must describe the run that would happen ---------------------


def test_the_dry_run_reports_the_requested_format_not_the_capability_guess(graph):
    """A dry run that described a different pipeline from the real one would be
    worse than no dry run: it is the check people trust before printing."""
    dual_capability(graph)
    graph.add_item("1", status=print_policy.READY)

    payload = as_json(post(SUBMIT, body(printFormat=PWG, dryRun=True)))

    conversion = payload["conversion"]
    assert payload["printFormat"] == PWG
    assert conversion["profile"] == "pdf-to-pwg-raster"
    assert conversion["uploadContentType"] == PWG
    assert conversion["requestedFormat"] == PWG
    assert conversion["conversionRequired"] is True
    assert graph.created_jobs() == [], "a dry run must not print"


def test_the_dry_run_without_a_format_still_reports_the_capability_choice(graph):
    dual_capability(graph)

    payload = as_json(post(SUBMIT, body(dryRun=True)))

    assert payload["printFormat"] is None
    assert payload["conversion"]["requestedFormat"] is None
    assert payload["conversion"]["profile"] == "passthrough"
    assert payload["conversion"]["conversionRequired"] is False


# --- the bench diagnostic must not drift from the route ----------------------
#
# printing/plan.py exists so a local test borrows the APP'S answer instead of
# inventing its own -- scripts/live-print-test.ps1 calls it to decide what to send
# by hand. It used to call select_profile directly, which was fine until
# printFormat existed: on a printer reporting BOTH formats the route would
# rasterize on request while plan.py still reported passthrough, and the bench
# test would have "proved" a pipeline the app does not run. Both now go through
# printing.profile_for. These tests are what keep that true.


def raster_only_capabilities():
    """The live printer: image/pwg-raster and nothing else (README, verified
    against the API 2026-08-31)."""
    return {"contentTypes": [PWG], "dpis": [300, 600], "scalings": ["fit"],
            "mediaSizes": ["North America Letter"],
            "topMargins": [4320], "bottomMargins": [4320],
            "leftMargins": [4320], "rightMargins": [4320]}


def dual_capabilities():
    return {"contentTypes": [PDF, PWG], "dpis": [300], "scalings": ["fit"],
            "mediaSizes": ["North America Letter"]}


@pytest.mark.parametrize("capabilities", [raster_only_capabilities(),
                                          dual_capabilities()])
@pytest.mark.parametrize("requested", ["", PDF, PWG])
def test_the_plan_tool_picks_the_same_profile_as_the_route(capabilities, requested):
    """One assertion, two code paths, every combination of printer and request.

    `_dry_run_conversion` is what the endpoint reports; `build_plan` is what the
    bench script reports. They must never disagree about which profile runs.
    """
    from printing import plan as plan_module
    import function_app

    share = plan_module.share_from_capabilities(capabilities)
    route = function_app._dry_run_conversion(share, requested)
    bench = plan_module.build_plan(capabilities, PDF, requested)

    assert bench["supported"] == route["supported"]
    assert bench.get("profile") == route.get("profile")
    assert bench.get("requestedFormat") == route.get("requestedFormat")
    if route["supported"]:
        assert bench["uploadContentType"] == route["uploadContentType"]
        assert bench["conversionRequired"] == route["conversionRequired"]


def test_the_plan_tool_honours_a_requested_format_over_the_capabilities():
    """The case that motivated the change: a dual-capability printer, where
    capability selection and an explicit request give different answers."""
    from printing import plan as plan_module

    capabilities = dual_capabilities()
    assert plan_module.build_plan(capabilities, PDF)["profile"] == "passthrough"
    assert plan_module.build_plan(capabilities, PDF, PWG)["profile"] == \
        "pdf-to-pwg-raster"


def test_the_plan_tool_explains_an_impossible_format_by_the_document():
    """Not "the printer does not accept it" -- the printer is fine."""
    from printing import plan as plan_module

    plan = plan_module.build_plan(dual_capabilities(), "image/jpeg", PWG)

    assert plan["supported"] is False
    assert "cannot produce" in plan["reason"]
