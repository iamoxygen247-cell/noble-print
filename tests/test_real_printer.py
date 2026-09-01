"""
test_real_printer.py — the pipeline against Noble's actual Universal Print
registration.

    Brother DCP-L2540DW series
      Printer Id  ffa65a34-615c-493b-9eff-d227133293ac   stable
      Share Id    a11f0263-68b7-45f4-b042-f1b4b30b60a3   re-shared 2026-08-31

RETIRED DEVICE, DELIBERATELY KEPT. On 2026-08-31 this printer was replaced by the
Brother MFC-L5800DW series [3c2af401eecf] (share 4429bf4e-..., printer
cf8d9fa1-...), because the DCP-L2540DW is not Universal Print ready.

This file is NOT stale. It is kept as the **PDF-passthrough** arm of the content
type mapping: a printer that reports application/pdf must be sent the document
untouched. The MFC reports image/pwg-raster only and takes the converting arm, so
between the two fixtures both branches stay covered by a real registration rather
than an invented one. Do not "update" these ids to the current printer -- that
would delete the passthrough case.

Both ids here also record how fast this pair moves. The share id is the volatile
half: deleting and re-creating the share mints a new one, which happened earlier
the same day (5de37377 -> a11f0263) and 404'd every place it was written down.
The printer id survives a re-share -- but not a device swap, which is what
happened hours later. The durable identifier is neither GUID; it is the display
name, which is why every recorded id needs the printer named beside it.

Everywhere else the suite uses invented ids ("share-guid", "printer-guid"). Those
are fine for logic, but they are lookalikes: swap one for the other and a failure
message reads almost identically. The real pair are two visibly different GUIDs,
and mixing them up is precisely defect F3 -- Resubmit cancelled against the wrong
one, got a 404, and (correctly) read that as "already gone", so it reported a
clean cancel while the original job stayed alive to print a second copy.

So this file exists to make one class of mistake loud:

    the SHARE id addresses jobs      /print/shares/{share}/jobs/...
    the PRINTER id addresses cancel  /print/printers/{printer}/jobs/{job}/cancel

These are resource identifiers, not credentials. They name objects inside the
tenant and are useless without a token; the share id is a routine request
parameter Power Automate passes on every call.
"""

from __future__ import annotations

import pytest

import print_policy
import universal_print
from fake_graph import load_fixture
from helpers import POLL, RESUBMIT, SUBMIT, as_json, iso, post

REGISTRATION = load_fixture("printer_brother_dcp_l2540dw.json")
PRINTER_ID = REGISTRATION["printer"]["id"]
SHARE_ID = REGISTRATION["share"]["id"]


@pytest.fixture
def noble(graph):
    """FakeGraph carrying the real registration."""
    graph.use_real_printer()
    return graph


def body(**overrides):
    merged = {"library": "Documents", "folder": "/Invoices/ToPrint",
              "printerShareId": SHARE_ID}
    merged.update(overrides)
    return merged


# --- the fixture matches what the portal actually shows -----------------------


def test_the_two_identifiers_are_genuinely_different():
    """If these ever collapse to the same value the tests below stop proving
    anything, so the premise is asserted rather than assumed."""
    assert PRINTER_ID == "ffa65a34-615c-493b-9eff-d227133293ac"
    assert SHARE_ID == "a11f0263-68b7-45f4-b042-f1b4b30b60a3"
    assert PRINTER_ID != SHARE_ID


def test_the_registration_matches_the_portal(noble):
    import graph_client

    client = graph_client.GraphClient(lambda: "token", session=noble)
    share = universal_print.get_share(client, SHARE_ID)

    assert share.share_id == SHARE_ID
    assert share.printer_id == PRINTER_ID
    assert share.display_name == "Brother DCP-L2540DW series"
    assert share.accepting_jobs is True, "the portal reports 'Is accepting jobs: Yes'"
    # 'idle', NOT 'ready'. The portal DISPLAYS "Ready"; the API returns "idle",
    # and printerProcessingState is documented as unknown|idle|processing|stopped
    # -- 'ready' is not a member of it. This assertion used to read "ready",
    # copied out of the portal, and pinned a value Graph never returns. Verified
    # against the live service by scripts/live-printer-check.ps1.
    assert share.state == "idle"


# --- submit addresses the SHARE ----------------------------------------------


def test_submit_creates_the_job_on_the_share_not_the_printer(noble):
    """POST /print/printers/{id}/jobs exists, but it is documented for printer
    ADMINISTRATORS doing preliminary testing. The share route is the supported
    one for ordinary users, and it is what the validated PowerShell used."""
    noble.add_item("1")

    post(SUBMIT, body())

    created = noble.created_jobs()
    assert len(created) == 1
    assert "/print/shares/{}/jobs".format(SHARE_ID) in created[0].url
    assert PRINTER_ID not in created[0].url, \
        "the job was addressed to the printer id instead of the share id"


def test_the_share_id_is_what_lands_in_the_printer_name_column(noble):
    """Printer_Name stores whatever the caller passed as printerShareId, because
    that is the value Poll and Resubmit need to look the job up again. Recorded
    as G2 in docs/design.md: the column is named for a name but holds an id."""
    noble.add_item("1")

    post(SUBMIT, body())

    assert noble.field("1", "Printer_Name") == SHARE_ID
    assert noble.field("1", "Printer_Name") != PRINTER_ID


def test_poll_looks_the_job_up_on_the_share(noble, frozen_now):
    noble.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer=SHARE_ID, created=iso(days=1))
    noble.add_job("1825", state="completed")

    post(POLL, {"library": "Documents", "folder": "/Invoices/ToPrint"})

    lookups = noble.calls_to("/jobs/1825", method="GET")
    assert lookups
    assert "/print/shares/{}/jobs/1825".format(SHARE_ID) in lookups[0].url
    assert noble.status_of("1") == print_policy.COMPLETED


# --- cancel addresses the PRINTER --------------------------------------------


def test_cancel_addresses_the_printer_id_not_the_share_id(noble, frozen_now):
    """THE F3 regression, with real ids so the failure message is unmistakable.

    Graph documents cancel only at /print/printers/{printerId}/jobs/{id}/cancel.
    Sending the SHARE id there 404s, and cancel_job treats 404 as "already gone",
    so the bug reported a successful cancel while the job stayed alive.
    """
    noble.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer=SHARE_ID, created=iso(hours=100))
    noble.add_job("1825", state="stopped", description="Out of paper")

    payload = as_json(post(RESUBMIT, body()))

    cancels = noble.calls_to("/cancel", method="POST")
    assert len(cancels) == 1
    url = cancels[0].url
    assert "/print/printers/{}/jobs/1825/cancel".format(PRINTER_ID) in url, url
    assert SHARE_ID not in url, \
        "cancel used the share id; Graph would 404 and the job would survive"
    assert payload["cancelled"] == 1
    assert payload["resubmitted"] == 1


def test_the_printer_id_is_never_used_to_address_a_job(noble):
    """Sweep every call: the printer id may appear ONLY on the cancel route."""
    noble.add_item("1", status=print_policy.PENDING, job_id="1825",
                   printer=SHARE_ID, created=iso(hours=100))
    noble.add_job("1825", state="stopped")

    post(RESUBMIT, body())

    for call in noble.calls:
        if PRINTER_ID in call.url:
            assert "/cancel" in call.url, \
                "printer id used outside the cancel route: {} {}".format(
                    call.method, call.url)


# --- content types: ANSWERED by the portal, both branches still pinned --------


def test_a_printer_that_reports_pdf_accepts_the_document(noble):
    """This printer's real case, verified from Properties > Printer defaults:
    Content type reads application/pdf."""
    noble.share["capabilities"]["contentTypes"] = ["application/pdf"]
    noble.add_item("1", name="invoice.pdf")

    assert as_json(post(SUBMIT, body()))["submitted"] == 1


def test_an_oxps_only_printer_fails_the_file_with_a_readable_reason(noble):
    """NOT this printer -- kept as a guard for the next one.

    The Brother reports application/pdf, so this branch is now hypothetical here.
    It stays because the utility is meant to be reused: some connector-attached
    printers report only application/oxps, and PDF cannot be converted to OXPS
    (Universal Print converts the other direction). When that printer appears,
    the honest outcome is a clear failure at preflight naming both types, rather
    than an opaque error out of the upload.
    """
    noble.share["capabilities"]["contentTypes"] = ["application/oxps"]
    noble.add_item("1", name="invoice.pdf")

    payload = as_json(post(SUBMIT, body()))

    assert payload["failed"] == 1
    message = noble.field("1", "Print_Message")
    assert "does not accept" in message
    assert "application/pdf" in message
    assert "application/oxps" in message, "the message must say what it DOES accept"
    assert noble.created_jobs() == [], "no job should be created for a rejected type"


def test_dry_run_reports_the_live_capabilities(noble):
    """The whole point of dryrun on this printer: it answers the contentTypes
    question against the real tenant, before anything is committed."""
    noble.add_item("1")

    printer = as_json(post(SUBMIT, body(dryRun=True)))["printer"]

    assert printer["shareId"] == SHARE_ID
    assert printer["printerId"] == PRINTER_ID
    assert printer["displayName"] == "Brother DCP-L2540DW series"
    assert printer["acceptingJobs"] is True
    assert "contentTypes" in printer


# --- the printer being unavailable -------------------------------------------


def test_a_not_ready_printer_stops_before_anything_is_claimed(noble):
    """The portal shows 'Is accepting jobs: Yes'. When it says No -- offline,
    out of paper, unregistered -- the queue must be left untouched."""
    noble.share["isAcceptingJobs"] = False
    noble.add_item("1")

    payload = as_json(post(SUBMIT, body()))

    assert payload["submitted"] == 0
    assert noble.status_of("1") == print_policy.READY
    assert not noble.calls_to("/items/1/fields", method="PATCH")


def test_a_full_round_trip_on_the_real_registration(noble, frozen_now):
    """Submit, then Poll, end to end, on the actual ids -- the closest the
    offline suite gets to the deploy smoke test in docs/design.md §11."""
    noble.add_item("1", name="Invoice 2026-08 Acme Ltd.pdf", created=iso(days=1))

    submitted = as_json(post(SUBMIT, body()))
    job_id = submitted["items"][0]["jobId"]

    assert noble.field("1", "Print_Status") == print_policy.PENDING
    assert noble.field("1", "Printer_Name") == SHARE_ID
    assert noble.field("1", "Print_JobId") == job_id

    noble.jobs[job_id]["status"]["state"] = "completed"
    post(POLL, {"library": "Documents", "folder": "/Invoices/ToPrint"})

    assert noble.field("1", "Print_Status") == print_policy.COMPLETED
    assert noble.field("1", "Print_Message").startswith("printed on ")
    assert noble.field("1", "Print_JobId") == job_id, "the audit trail must survive"
    assert noble.field("1", "Printer_Name") == SHARE_ID


# --- the printer's own defaults, verified from Properties > Printer defaults ---


DEFAULTS = REGISTRATION["share"]["defaults"]
CAPABILITIES = REGISTRATION["share"]["capabilities"]


def test_the_printer_accepts_pdf():
    """VERIFIED against the live API, not a screenshot.

    The question that matters is "will this device take the PDFs we pull out of
    SharePoint" -- and it will, with no OXPS conversion.

    What this test used to get wrong: it asserted the capability list was
    EXACTLY ["application/pdf"], read off the portal's Properties > Printer
    defaults page. That page shows the DEFAULT content type. The capability list
    is a different field and holds two entries. So the assertion is on
    membership, which is the real requirement, rather than on an exact list that
    was never the API's answer."""
    assert "application/pdf" in CAPABILITIES["contentTypes"]
    assert "application/oxps" in CAPABILITIES["contentTypes"]
    # The DEFAULT is still pdf -- a different field, and this one the portal
    # really was showing.
    assert DEFAULTS["contentType"] == "application/pdf"


def test_the_job_configuration_we_send_is_supported_by_this_printer(noble):
    """THE guard on JOB_CONFIGURATION.

    We deliberately send only `copies` and let the device's own defaults pick
    colour, duplex, orientation and quality. This asserts that what we DO send is
    something this printer accepts -- so if someone later adds `duplexMode` or
    `fitPdfToPage`, both of which this device does NOT support (they are greyed
    out in the portal), the test fails here rather than at the printer.
    """
    sent = print_policy.JOB_CONFIGURATION

    assert set(sent) == {"copies"}, (
        "JOB_CONFIGURATION grew beyond `copies`: {}. Check every new key against "
        "this printer's capabilities before shipping it.".format(sorted(sent)))

    copies_range = CAPABILITIES["copiesPerJob"]
    assert copies_range["start"] <= sent["copies"] <= copies_range["end"]

    # The two settings the portal greys out must never appear.
    assert "fitPdfToPage" not in sent
    assert "multipageLayout" not in sent
    assert CAPABILITIES["supportsFitPdfToPage"] is False


def test_the_submitted_job_body_carries_only_copies(noble):
    """What actually goes on the wire, not just the constant."""
    noble.add_item("1")

    post(SUBMIT, body())

    created = noble.created_jobs()[0]
    assert created.body == {"configuration": {"copies": 1}}


def test_copies_matches_the_printers_own_default(noble):
    """The portal's "Copies per job: 1" and our JOB_CONFIGURATION agree, so the
    value we send can never contradict the device default."""
    assert print_policy.JOB_CONFIGURATION["copies"] == DEFAULTS["copiesPerJob"]


def test_this_printer_prints_single_sided_greyscale(noble):
    """Not a requirement, recorded because it is a real operational fact: this
    device defaults to one-sided greyscale, so a 40-page invoice batch is 40
    sheets. Changing it is a printer-side setting, not a code change -- which is
    exactly why the design leaves duplex and colour to the device."""
    assert DEFAULTS["duplexMode"] == "oneSided"
    assert DEFAULTS["colorMode"] == "grayscale"
    assert CAPABILITIES["isColorPrintingSupported"] is False
    # We send neither, so the device defaults win.
    assert "duplexMode" not in print_policy.JOB_CONFIGURATION
    assert "colorMode" not in print_policy.JOB_CONFIGURATION


# --- the connector blade is empty, and that is fine ---------------------------


def test_no_connector_is_registered_and_the_app_does_not_care():
    """The Connectors blade reads "No rows to display" -- meaning UNKNOWN.

    An empty list is consistent with two opposite situations: a Universal
    Print-ready printer registered directly (fine, no connector needed), or a
    non-UP-ready printer whose connector is missing (jobs accepted, never
    delivered). The Overview blade cannot separate them, because a connector's
    heartbeat updates "last seen" exactly as a native printer's does.

    scripts/live-printer-check.ps1 settles it by printing a page. Until then this
    asserts only what is actually known: the list is empty, and the app does not
    read it -- listing connectors needs the PrintConnector.Read.All scope and says
    less than isAcceptingJobs and status.state, which the preflight already reads.
    """
    assert REGISTRATION["connectors"]["value"] == []
    assert "PrintConnector.Read.All" not in " ".join(
        __import__("graph_auth").SCOPES), \
        "an extra admin-consented scope was added for a weaker signal"


def test_the_preflight_reads_the_signals_that_do_matter(noble):
    """isAcceptingJobs and status.state are what tell us the device will take
    work -- and unlike the connector list, they need no extra permission."""
    import graph_client

    client = graph_client.GraphClient(lambda: "token", session=noble)
    share = universal_print.get_share(client, SHARE_ID)

    assert share.accepting_jobs is True
    assert share.state == "idle"  # the API's value; the portal shows "Ready"
