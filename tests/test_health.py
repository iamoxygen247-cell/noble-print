"""
test_health.py — the pre-flight endpoint Power Automate calls before working.

Health exists because every way this pipeline fails was previously discovered
AFTER a flow had started, and several only after files had been claimed: an
offline printer is a Submit 200 with `failed = 0` (defect S3); a dead refresh
token 500s everything; a `pypdfium2` wheel that did not install is found one file
at a time, each row going PRINT_PENDING then PRINT_FAILED.

Two tiers in one file, because they describe one feature:

  A  print_policy.health_findings -- pure rules, no fake, no HTTP
  C  the route, through FakeGraph

THE DISTINCTION MOST OF THIS FILE DEFENDS: a malformed REQUEST is a 400, an
unhealthy PRINTER is a 200 with `healthy: false`. Conflating them would have a
flow notifying about a broken device over its own typo, and would make the body --
which is the entire diagnosis -- awkward for Power Automate to read, because a
non-2xx marks the HTTP action as failed and halts the branch.
"""

from __future__ import annotations

import logging

import pytest

import graph_auth
import print_policy
import universal_print
from helpers import HEALTH, SITE, SUBMIT, as_json, post, print_events, run_summaries

PDF = "application/pdf"
PWG = "image/pwg-raster"

BODY = {"printerShareId": "share-guid"}


def body(**overrides):
    merged = dict(BODY)
    merged.update(overrides)
    return merged


# --- Tier A: the rules ---------------------------------------------------------


def findings(**overrides):
    """A healthy raster-only printer, unless a keyword says otherwise.

    Defaults describe the real device: `image/pwg-raster` and nothing else, so a
    PDF is converted. Each test changes exactly one thing, which is what makes a
    failure point at a cause.
    """
    kwargs = dict(
        accepting_jobs=True,
        state=print_policy.PRINTER_STATE_IDLE,
        content_types=[PWG],
        printer_id="printer-guid",
        print_format=PWG,
        format_supported=True,
        profile_name="pdf-to-pwg-raster",
        conversion_required=True,
        converter_available=True,
        configuration_error="",
    )
    kwargs.update(overrides)
    return print_policy.health_findings(**kwargs)


def codes(items):
    return [item["code"] for item in items]


def test_a_healthy_raster_printer_reports_nothing(  # A1
):
    errors, warnings = findings()

    assert errors == []
    assert warnings == []


def test_a_printer_not_accepting_jobs_is_an_error(  # A2
):
    """Universal Print refuses the job outright, so submitting is pointless."""
    errors, _ = findings(accepting_jobs=False)

    assert print_policy.HEALTH_PRINTER_NOT_ACCEPTING_JOBS in codes(errors)


def test_a_stopped_printer_is_an_error(  # A3
):
    """By explicit decision. A stopped device may still QUEUE work that prints on
    recovery -- UC-8 shows an unplugged printer doing exactly that -- so this
    deliberately halts Flow A rather than letting documents pile up against a
    faulty printer. The trade is recorded in the plan and in design.md."""
    errors, _ = findings(state=print_policy.PRINTER_STATE_STOPPED)

    assert print_policy.HEALTH_PRINTER_STOPPED in codes(errors)


@pytest.mark.parametrize("state", [print_policy.PRINTER_STATE_IDLE,
                                   print_policy.PRINTER_STATE_PROCESSING])
def test_idle_and_processing_are_healthy(state):  # A4
    """`processing` means it is printing something. That is the opposite of a
    fault, and reading it as one would stop the queue mid-job."""
    errors, warnings = findings(state=state)

    assert errors == []
    assert warnings == []


@pytest.mark.parametrize("state", ["", "unknown", "  UNKNOWN  "])
def test_an_absent_or_unknown_state_is_only_a_warning(state):  # A5
    """Not evidence of a fault -- evidence of a device that does not say. This
    field has misled once already: a fixture and two tests pinned the portal's
    display string "Ready" as an API value (design.md)."""
    errors, warnings = findings(state=state)

    assert errors == []
    assert codes(warnings) == [print_policy.HEALTH_PRINTER_STATE_UNKNOWN]


def test_a_share_with_no_printer_id_is_an_error(  # A6
):
    """Cancel is documented ONLY on /print/printers/{id}/jobs/{id}/cancel. With
    no printer id, Poll cannot cancel a stalled job before requeuing it and the
    original prints alongside its replacement -- CLAUDE.md rule 2, defect D1.
    Nothing else in the app checks this."""
    errors, _ = findings(printer_id="")

    assert print_policy.HEALTH_NO_PRINTER_ID in codes(errors)
    finding = [e for e in errors
               if e["code"] == print_policy.HEALTH_NO_PRINTER_ID][0]
    assert "duplicate" in finding["message"], \
        "the message must say what actually goes wrong, not just what is missing"


def test_a_format_the_printer_does_not_report_is_an_error(  # A7
):
    errors, _ = findings(print_format=PDF, format_supported=False,
                         content_types=[PWG])

    finding = [e for e in errors
               if e["code"] == print_policy.HEALTH_FORMAT_NOT_SUPPORTED][0]
    assert PDF in finding["message"]
    assert PWG in finding["message"], "the message must say what IS supported"


@pytest.mark.parametrize("absent", [None, ""])
def test_no_usable_profile_is_an_error(absent):  # A8
    """`None` is what the conversion report actually carries. `""` is covered too
    because an identity check would let an empty name through and report a healthy
    printer with no way to print -- the worst answer this endpoint could give."""
    errors, _ = findings(profile_name=absent)

    assert print_policy.HEALTH_NO_PROFILE in codes(errors)


def test_a_job_configuration_that_cannot_be_built_is_an_error(  # A9
):
    """Every submission would fail at the same point."""
    errors, _ = findings(configuration_error="mediaSize is not supported")

    finding = [e for e in errors
               if e["code"] == print_policy.HEALTH_JOB_CONFIGURATION_FAILED][0]
    assert "mediaSize" in finding["message"]


def test_a_missing_converter_is_an_error_when_conversion_is_needed(  # A10
):
    """Otherwise found one file at a time, after each has been claimed."""
    errors, _ = findings(conversion_required=True, converter_available=False)

    finding = [e for e in errors
               if e["code"] == print_policy.HEALTH_CONVERTER_UNAVAILABLE][0]
    assert "pypdfium2" in finding.get("remedy", "")


def test_a_missing_converter_is_ignored_when_nothing_converts(  # A11
):
    """THE GUARD THAT MATTERS HERE. A passthrough printer never loads the
    renderer, so a missing one is not its problem -- and reporting it would make
    every PDF-capable printer permanently unhealthy on a host that legitimately
    has no rasterizer."""
    errors, warnings = findings(profile_name="passthrough", print_format=PDF,
                                content_types=[PDF], conversion_required=False,
                                converter_available=False)

    assert errors == []
    assert warnings == []


def test_a_printer_reporting_no_content_types_is_only_a_warning(  # A12
):
    """ShareInfo.supports deliberately gives an under-reporting device the
    benefit of the doubt, so the app will still try. Health must not be stricter
    than the endpoint it predicts."""
    errors, warnings = findings(content_types=[], format_supported=True)

    assert errors == []
    assert codes(warnings) == [print_policy.HEALTH_NO_CONTENT_TYPES]


def test_healthy_is_exactly_the_absence_of_errors(  # A13
):
    assert findings()[0] == []
    assert findings(accepting_jobs=False)[0] != []


def test_warnings_alone_never_make_a_printer_unhealthy(  # A14
):
    errors, warnings = findings(content_types=[], state="unknown",
                                format_supported=True)

    assert warnings, "this fixture is meant to produce warnings"
    assert errors == [], "warnings must never become errors"


def test_every_health_code_is_unique(  # A15
):
    """Two constants sharing a string would make one of them permanently
    unreportable, and nothing else would fail."""
    assert len(set(print_policy.ALL_HEALTH_CODES)) == \
        len(print_policy.ALL_HEALTH_CODES)


def test_the_printer_stopped_state_is_not_the_job_stopped_state(  # A16
):
    """THE NAMING TRAP. printJobStatus.state and printerProcessingState BOTH
    spell "stopped" and mean different things:

        JOB_STOPPED            one job is blocked; it CAN still continue, which
                               is why rule 2 cancels before replacing
        PRINTER_STATE_STOPPED  the device itself reports a fault

    They happen to hold the same string today. This test does not forbid that --
    it forbids the two being the SAME CONSTANT, so that changing one can never
    silently change the other.
    """
    assert print_policy.JOB_STOPPED == print_policy.PRINTER_STATE_STOPPED, \
        "they DO spell the same word -- that is the trap, not a bug"

    # The guard that bites: each enum holds exactly its own documented members.
    # Merging them, or reusing one constant for both, changes one of these sets.
    assert set(print_policy.ALL_PRINTER_STATES) == {
        "unknown", "idle", "processing", "stopped"}, \
        "printerProcessingState is documented unknown|idle|processing|stopped"
    assert len(print_policy.ALL_JOB_STATES) == 8, \
        "printJobStatus.state has eight documented values"

    # NEITHER enum contains the other, which is the point: they overlap on three
    # words and diverge on the rest, so a reader who assumes one is a superset of
    # the other is wrong in both directions.
    jobs = set(print_policy.ALL_JOB_STATES)
    printers = set(print_policy.ALL_PRINTER_STATES)
    assert jobs & printers == {"unknown", "processing", "stopped"}, \
        "the three words that mean different things depending on which enum"
    assert printers - jobs == {"idle"}, "idle is a PRINTER state only"
    assert jobs - printers == {"pending", "paused", "completed", "canceled",
                               "aborted"}, "these are JOB states only"


def test_every_finding_is_reported_not_just_the_first(  # A17
):
    """A flow told one problem per ten-minute cycle takes an hour to learn six."""
    errors, warnings = findings(
        accepting_jobs=False, state=print_policy.PRINTER_STATE_STOPPED,
        printer_id="", print_format=PDF, format_supported=False,
        profile_name=None, conversion_required=True, converter_available=False,
        configuration_error="broken")

    assert set(codes(errors)) == {
        print_policy.HEALTH_PRINTER_NOT_ACCEPTING_JOBS,
        print_policy.HEALTH_PRINTER_STOPPED,
        print_policy.HEALTH_NO_PRINTER_ID,
        print_policy.HEALTH_FORMAT_NOT_SUPPORTED,
        print_policy.HEALTH_NO_PROFILE,
        print_policy.HEALTH_JOB_CONFIGURATION_FAILED,
        print_policy.HEALTH_CONVERTER_UNAVAILABLE,
    }


def test_the_message_names_the_codes_when_unhealthy():
    message = print_policy.health_message(
        [{"code": "A"}, {"code": "B"}], [], "Front Office", "passthrough")

    assert "2 problems" in message and "A, B" in message


def test_the_message_names_the_printer_when_healthy():
    message = print_policy.health_message([], [], "Front Office", "passthrough")

    assert "Front Office" in message and "passthrough" in message


# --- Tier C: the route ---------------------------------------------------------


def raster_only(graph):
    """The live printer: image/pwg-raster and nothing else (README, verified
    against the API 2026-08-31)."""
    graph.use_capabilities(contentTypes=[PWG], dpis=[300, 600],
                           scalings=["fit"], mediaSizes=["North America Letter"],
                           topMargins=[4320], bottomMargins=[4320],
                           leftMargins=[4320], rightMargins=[4320])


def test_a_healthy_printer_reports_healthy(graph):  # C1
    raster_only(graph)

    payload = as_json(post(HEALTH, body(printFormat=PWG)))

    assert payload["healthy"] is True
    assert payload["errors"] == []
    assert payload["conversion"]["profile"] == "pdf-to-pwg-raster"
    assert payload["printer"]["printerId"] == graph.PRINTER_ID


@pytest.mark.parametrize("request_body", [
    {"printerShareId": "share-guid"},
    {"printerShareId": "share-guid", "printFormat": "image/pwg-raster"},
    {"printerShareId": "no-such-share"},
])
def test_every_200_carries_healthy_errors_and_warnings(graph, request_body):  # C2
    """THE S3 LESSON. An offline printer used to be a 200 that matched no flow
    condition at all. These three keys are present on every answer so a Power
    Automate condition never needs a null check."""
    response = post(HEALTH, request_body)

    assert response.status_code == 200
    payload = as_json(response)
    assert isinstance(payload["healthy"], bool)
    assert isinstance(payload["errors"], list)
    assert isinstance(payload["warnings"], list)


def test_an_unknown_share_is_reported_as_not_found(graph):  # C3
    payload = as_json(post(HEALTH, body(printerShareId="no-such-share")))

    assert payload["healthy"] is False
    assert print_policy.HEALTH_PRINTER_NOT_FOUND in \
        [e["code"] for e in payload["errors"]]


def test_a_not_found_share_carries_the_stale_id_remedy(graph):  # C3b
    """The share id is the perishable half of the share/printer pair. A 404 is
    almost always a re-created share, and the remedy says where to get the new
    one -- including that the Flow bodies hold a copy."""
    payload = as_json(post(HEALTH, body(printerShareId="no-such-share")))

    remedy = payload["errors"][0].get("remedy", "")
    assert "Universal Print" in remedy and "flows" in remedy


def test_a_graph_failure_is_reported_as_unreachable(graph):  # C4
    graph.fail_next("GET", "/print/shares", status=500, times=4)

    payload = as_json(post(HEALTH, body()))

    assert payload["healthy"] is False
    assert print_policy.HEALTH_PRINTER_UNREACHABLE in \
        [e["code"] for e in payload["errors"]]


def test_a_dead_refresh_token_is_reported_with_its_remedy(graph, monkeypatch):  # C5
    """The total-failure mode: without this, the first symptom is every Submit
    and Poll 500ing. The token provider is called lazily inside the first
    request, so this surfaces from get_share, not from building the client."""
    def dead():
        raise graph_auth.AuthBootstrapRequired("refresh token rejected")

    monkeypatch.setattr(graph_auth, "get_access_token", dead)

    payload = as_json(post(HEALTH, body()))

    assert payload["healthy"] is False
    finding = payload["errors"][0]
    assert finding["code"] == print_policy.HEALTH_AUTH_BOOTSTRAP_REQUIRED
    assert "bootstrap_token.py" in finding["remedy"]


def test_an_unreadable_share_returns_null_blocks_and_stops(graph):  # C6
    """Without a share there is nothing to evaluate, so a partial picture would
    be a misleading one: exactly one error, and no invented printer block."""
    payload = as_json(post(HEALTH, body(printerShareId="no-such-share")))

    assert payload["printer"] is None
    assert payload["conversion"] is None
    assert len(payload["errors"]) == 1


def test_an_offline_printer_is_unhealthy(graph):  # C7
    graph.share["isAcceptingJobs"] = False

    payload = as_json(post(HEALTH, body()))

    assert payload["healthy"] is False
    assert print_policy.HEALTH_PRINTER_NOT_ACCEPTING_JOBS in \
        [e["code"] for e in payload["errors"]]


def test_a_format_the_printer_refuses_is_unhealthy_not_a_400(graph):  # C8
    """The printer is the problem, not the request. This must stay a 200 so the
    flow reads the body and notifies, rather than the HTTP action failing."""
    raster_only(graph)

    response = post(HEALTH, body(printFormat=PDF))

    assert response.status_code == 200
    payload = as_json(response)
    assert payload["healthy"] is False
    assert print_policy.HEALTH_FORMAT_NOT_SUPPORTED in \
        [e["code"] for e in payload["errors"]]


def test_an_unknown_format_is_a_400_not_an_unhealthy_printer(graph):  # C9
    """THE DISTINCTION THAT MATTERS. A typo in the flow is the caller's error and
    says nothing about the device. Answering 200/unhealthy would have somebody
    checking the printer over their own request."""
    response = post(HEALTH, body(printFormat="image/png"))

    assert response.status_code == 400
    assert "not supported" in as_json(response)["error"]
    assert "healthy" not in as_json(response)
    assert graph.calls == [], \
        "a rejected request must not cost a Graph call -- validation comes first"


@pytest.mark.parametrize("request_body", [{}, {"printerShareId": ""},
                                          {"printerShareId": "   "}])
def test_a_missing_printer_share_id_is_a_400(graph, request_body):  # C10
    response = post(HEALTH, request_body)

    assert response.status_code == 400
    assert graph.calls == [], "validation runs before any Graph call"


def test_several_faults_are_all_reported_at_once(graph):  # C11
    graph.share["isAcceptingJobs"] = False
    graph.share["status"] = {"state": "stopped"}
    graph.share["printer"] = {}

    payload = as_json(post(HEALTH, body()))

    assert set([e["code"] for e in payload["errors"]]) >= {
        print_policy.HEALTH_PRINTER_NOT_ACCEPTING_JOBS,
        print_policy.HEALTH_PRINTER_STOPPED,
        print_policy.HEALTH_NO_PRINTER_ID,
    }


def test_health_makes_exactly_one_graph_call(graph):  # C12
    """It runs on every flow tick, before both other endpoints. One share read is
    the entire budget; anything more and it stops being free to call."""
    post(HEALTH, body())

    assert len(graph.calls) == 1
    assert graph.calls[0].method == "GET"
    assert "/print/shares/" in graph.calls[0].url


def test_health_never_writes_anything(graph):  # C13
    """A pre-flight check that touched the queue could not safely be called
    before every run."""
    graph.share["isAcceptingJobs"] = False

    post(HEALTH, body())

    assert [c for c in graph.calls if c.method in ("PATCH", "POST", "DELETE")] == []


def test_the_run_summary_reports_the_counts(graph, caplog):  # C14
    graph.share["isAcceptingJobs"] = False
    graph.share["status"] = {"state": "unknown"}

    with caplog.at_level(logging.INFO):
        post(HEALTH, body())

    summary = [s for s in run_summaries(caplog) if s["ep"] == "health"][0]
    assert summary["printer"] == "share-guid"
    assert summary["ok"] == "0", "ok is 1 when healthy, 0 when not"
    assert summary["failed"] == "1", "failed counts errors"
    assert summary["skipped"] == "1", "skipped counts warnings"
    assert summary["httpStatus"] == "200"


def test_health_emits_no_print_event(graph, caplog):  # C15
    """PRINT_EVENT is one line per FILE per outcome, and Health touches no files.
    Its absence is correct, not an oversight -- do not 'fix' it."""
    with caplog.at_level(logging.INFO):
        post(HEALTH, body())

    assert print_events(caplog) == []


# --- parity: Health must describe the pipeline Submit would run ----------------
#
# The same guard that now pins printing/plan.py. Health is only worth calling if
# it predicts what Submit does; two endpoints describing one printer differently
# is the failure mode, not a cosmetic inconsistency.

SUBMIT_BODY = {**SITE, "library": "Documents", "folder": "/Invoices/ToPrint",
               "printerShareId": "share-guid", "dryRun": True}


@pytest.mark.parametrize("print_format", ["", PWG])
def test_the_printer_block_matches_submits_dry_run(graph, print_format):  # P1
    raster_only(graph)
    extra = {"printFormat": print_format} if print_format else {}

    health = as_json(post(HEALTH, body(**extra)))
    dry_run = as_json(post(SUBMIT, dict(SUBMIT_BODY, **extra)))

    assert health["printer"] == dry_run["printer"]


@pytest.mark.parametrize("print_format", ["", PWG])
def test_the_conversion_block_matches_submits_dry_run(graph, print_format):  # P2
    raster_only(graph)
    extra = {"printFormat": print_format} if print_format else {}

    health = as_json(post(HEALTH, body(**extra)))
    dry_run = as_json(post(SUBMIT, dict(SUBMIT_BODY, **extra)))

    assert health["conversion"] == dry_run["conversion"]


# --- the stale-share remedy on Submit ------------------------------------------


def test_a_stale_share_id_on_submit_names_the_remedy(graph):  # S1
    """Health reports this as PRINTER_NOT_FOUND. This is for whoever calls Submit
    without checking first: design.md recorded the missing remedy long before it
    was written, and without it the answer is a bare GraphError naming no action.

    Poll cannot reach this path -- universal_print.get_job uses
    allow_status=(404,) and returns None, so a stale share there is `not_found`,
    never a 500.
    """
    response = post(SUBMIT, {**SITE, "library": "Documents", "folder": "",
                             "printerShareId": "no-such-share"})

    assert response.status_code == 500
    payload = as_json(response)
    assert "Universal Print" in payload["remedy"]
    assert graph.created_jobs() == [], "nothing may be submitted on this path"


# --- the two route wirings that had no test ------------------------------------
#
# Both codes were covered at Tier A, where the flags are passed in by hand. That
# proves the RULE and nothing about the ROUTE: whether function_app actually asks
# the questions, and asks them only when they matter. _converter_available was
# made a module-level function specifically so it could be patched here.


def test_a_missing_renderer_makes_a_raster_printer_unhealthy(graph, monkeypatch):
    """The failure this endpoint exists to move earlier. Without it the first
    symptom is one claimed row at a time going PRINT_PENDING then PRINT_FAILED,
    because pwg_converter imports pypdfium2 lazily inside convert_pdf."""
    import function_app

    raster_only(graph)
    monkeypatch.setattr(function_app, "_converter_available", lambda: False)

    payload = as_json(post(HEALTH, body(printFormat=PWG)))

    assert payload["healthy"] is False
    finding = [e for e in payload["errors"]
               if e["code"] == print_policy.HEALTH_CONVERTER_UNAVAILABLE][0]
    assert "pypdfium2" in finding["remedy"]


def test_a_missing_renderer_is_ignored_by_a_printer_that_takes_pdf(graph,
                                                                  monkeypatch):
    """THE HALF THAT WOULD HAVE GONE UNNOTICED. A passthrough printer never loads
    the renderer, so a missing one must not make it unhealthy -- otherwise every
    PDF-capable printer reports a fault on a host that legitimately has no
    rasterizer. The route must ask only when a conversion would actually run."""
    import function_app

    graph.use_capabilities(contentTypes=[PDF])
    monkeypatch.setattr(function_app, "_converter_available", lambda: False)

    payload = as_json(post(HEALTH, body(printFormat=PDF)))

    assert payload["healthy"] is True
    assert payload["conversion"]["conversionRequired"] is False


def test_the_renderer_is_not_even_loaded_when_nothing_converts(graph, monkeypatch):
    """Loading pypdfium2 costs ~70 ms on a cold start. A printer that needs no
    conversion should not pay it, and the guard is `if conversion_required`."""
    import function_app

    calls = []
    graph.use_capabilities(contentTypes=[PDF])
    monkeypatch.setattr(function_app, "_converter_available",
                        lambda: calls.append(1) or True)

    post(HEALTH, body(printFormat=PDF))

    assert calls == [], "the renderer was loaded for a passthrough printer"


def test_a_job_configuration_that_cannot_be_built_is_reported(graph, monkeypatch):
    """`_dry_run_conversion` catches this and reports `configurationError`; the
    route has to read it back out. That one line was untested -- the branch is
    even marked `# pragma: no cover` in function_app."""
    from printing import profiles

    raster_only(graph)

    def explode(self, share):
        raise RuntimeError("mediaSize North America Letter is not supported")

    monkeypatch.setattr(profiles.PwgRasterProfile, "job_configuration", explode)

    payload = as_json(post(HEALTH, body(printFormat=PWG)))

    assert payload["healthy"] is False
    finding = [e for e in payload["errors"]
               if e["code"] == print_policy.HEALTH_JOB_CONFIGURATION_FAILED][0]
    assert "mediaSize" in finding["message"]


def test_health_needs_no_site_and_no_library(graph):
    """Health reads one printer share and resolves no SharePoint list, so the site
    parameters Submit and Poll now require do not apply to it.

    That is what makes it the signal during the cutover: between the deploy and the
    flows being updated, Submit and Poll answer 400 for a missing site while Health
    keeps answering truthfully about the printer.
    """
    response = post(HEALTH, {"printerShareId": graph.SHARE_ID})

    assert response.status_code == 200
    assert as_json(response)["healthy"] is True
    assert not graph.calls_to("/lists/"), "Health must not resolve a library"
