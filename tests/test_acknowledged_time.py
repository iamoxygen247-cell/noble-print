"""
test_acknowledged_time.py — `printed on ...` uses the printer's own timestamp.

FOUND BY LOOKING AT A REAL JOB (2026-08-30). Reading job 6 back out of the live
service showed a field the design had asserted did not exist:

    createdDateTime       2026-08-31T05:24:37Z
    acknowledgedDateTime  2026-08-31T05:25:03Z      <-- this one
    isFetchable           False
    errorCode             0

The design said Universal Print "carries no completion timestamp", and strictly
that is still true -- Microsoft documents `acknowledgedDateTime` only as "the
dateTimeOffset when the job was acknowledged", and there is no completion field
on printJob. But the claim had been used to justify something weaker than it
supports: stamping Print_Message with THE MOMENT POLL HAPPENED TO LOOK.

The difference is not academic. On the measured job, acknowledgement landed 26 s
after creation and roughly 10 s before the page finished. Poll runs every ten
MINUTES, so the observed time can be most of a polling interval late, and it
moves if you change the schedule -- it is a property of our cron, not of the
print. The acknowledgement is a property of the job: stable, reproducible, and
within seconds of the paper.

So: prefer acknowledgedDateTime, fall back to the observed time when it is
missing, and keep saying "printed on" either way -- the requirement's own words.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import print_policy
import universal_print
from helpers import NOW, POLL, SITE, as_json, iso, post

BODY = {**SITE, "library": "Documents", "folder": "/Invoices/ToPrint"}

# The real job 6, verbatim.
ACK_ISO = "2026-08-31T05:25:03Z"
ACK_UTC = datetime(2026, 8, 31, 5, 25, 3, tzinfo=timezone.utc)


# --- the policy ---------------------------------------------------------------


def test_completion_time_prefers_the_printers_acknowledgement():
    observed = NOW
    assert print_policy.completion_time(ACK_ISO, observed) == ACK_UTC


def test_completion_time_falls_back_to_the_observed_moment():
    """A job the printer never acknowledged has no better answer available, and
    a missing timestamp must never cost us the whole status write."""
    for missing in (None, "", "   ", "not-a-date"):
        assert print_policy.completion_time(missing, NOW) == NOW


def test_the_acknowledgement_is_rendered_in_the_business_time_zone():
    """05:25 UTC on the 31st is 22:25 on the 30th in Vancouver. The window maths
    stays in UTC; only the message is local."""
    message = print_policy.printed_on_message(
        print_policy.completion_time(ACK_ISO, NOW))
    assert message == "printed on 2026-08-30 22:25:03"


def test_the_message_format_is_unchanged():
    """The requirement's example is 'printed on 2026-08-01 14:23:23'. Whichever
    timestamp is chosen, the shape the caller sees must not move."""
    import re
    for source in (ACK_ISO, None):
        message = print_policy.printed_on_message(
            print_policy.completion_time(source, NOW))
        assert re.fullmatch(r"printed on \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}",
                            message), message


# --- the adapter --------------------------------------------------------------


def test_the_adapter_reads_acknowledged_date_time():
    assert universal_print.job_acknowledged_at(
        {"acknowledgedDateTime": ACK_ISO}) == ACK_ISO
    assert universal_print.job_acknowledged_at({}) is None
    assert universal_print.job_acknowledged_at(None) is None


def test_the_adapter_reads_created_date_time():
    """The other field the same live job exposed, and the one Poll's stall clock
    runs on. Absent reads as None rather than as zero, so a job whose age cannot
    be established is left alone instead of cancelled."""
    assert universal_print.job_created_at(
        {"createdDateTime": "2026-08-31T05:24:37Z"}) == "2026-08-31T05:24:37Z"
    assert universal_print.job_created_at({}) is None
    assert universal_print.job_created_at(None) is None


def test_the_two_timestamps_are_not_confused():
    """Both are on the same object and 26 seconds apart on the measured job.
    Reading the wrong one would make every fresh job look stalled, or every
    stalled job look fresh."""
    job = {"createdDateTime": "2026-08-31T05:24:37Z",
           "acknowledgedDateTime": ACK_ISO}
    assert universal_print.job_created_at(job) != universal_print.job_acknowledged_at(job)


# --- Poll ---------------------------------------------------------------------


def test_poll_stamps_the_acknowledgement_not_the_moment_it_looked(graph, frozen_now):
    graph.add_item("1", status=print_policy.PENDING, job_id="1801",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("1801", state="completed", acknowledged=ACK_ISO)

    as_json(post(POLL, BODY))

    assert graph.field("1", "Print_Message") == "printed on 2026-08-30 22:25:03"


def test_poll_falls_back_when_the_job_was_never_acknowledged(graph, frozen_now):
    """Unacknowledged and completed is odd, but it must still write a message --
    losing the status write over a missing optional field would strand the file
    in PRINT_PENDING forever."""
    graph.add_item("1", status=print_policy.PENDING, job_id="1801",
                   printer=graph.SHARE_ID, created=iso(days=1))
    graph.add_job("1801", state="completed")  # no acknowledgedDateTime

    as_json(post(POLL, BODY))

    assert graph.field("1", "Print_Message").startswith("printed on ")
    assert graph.status_of("1") == print_policy.COMPLETED


def test_two_jobs_completing_in_one_run_get_their_own_timestamps(graph, frozen_now):
    """The F5 property, now anchored to the printer rather than to the clock:
    two jobs acknowledged fifteen minutes apart must not share a timestamp."""
    later = "2026-08-31T05:39:31Z"  # the real job 9
    for item_id, job_id, ack in (("1", "1801", ACK_ISO), ("2", "1802", later)):
        graph.add_item(item_id, status=print_policy.PENDING, job_id=job_id,
                       printer=graph.SHARE_ID, created=iso(days=1))
        graph.add_job(job_id, state="completed", acknowledged=ack)

    as_json(post(POLL, BODY))

    assert graph.field("1", "Print_Message") == "printed on 2026-08-30 22:25:03"
    assert graph.field("2", "Print_Message") == "printed on 2026-08-30 22:39:31"


# --- one writer, so nothing can disagree --------------------------------------


def test_a_late_completion_beyond_every_deadline_still_uses_the_printers_time(
        graph, frozen_now):
    """This used to assert that Resubmit's `completed_late` branch wrote the same
    message as Poll -- two code paths on one column had to agree or the audit
    trail would differ depending on which endpoint arrived first. Poll is now the
    only writer, so that class of disagreement is gone by construction.

    What still needs pinning is the ORDERING inside poll_decision: a job that
    completed after the give-up deadline is recorded as printed, with the
    printer's own timestamp, not failed. The paper came out.
    """
    graph.add_item("1", status=print_policy.PENDING, job_id="1801",
                   printer=graph.SHARE_ID, created=iso(days=30))
    graph.add_job("1801", state="completed", acknowledged=ACK_ISO,
                  created=iso(days=30))

    payload = as_json(post(POLL, BODY))

    assert payload["completed"] == 1
    assert payload["gaveUp"] == 0, "a job that printed is never a failure"
    assert graph.field("1", "Print_Message") == "printed on 2026-08-30 22:25:03"
    assert len(graph.created_jobs()) == 0, "a completed job must never reprint"
