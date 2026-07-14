"""Edge-case coverage for connect()'s observer wiring (spec §12 + facade): on_report
and on_trace must fire identically regardless of consumption style (fetch_new/run/
stream), and on_trace must still report a dropped decision even when on_filtered=
"drop" hides the email from delivery (the audit guarantee). Mirrors the setup in
tests/test_connect_logging.py."""

from __future__ import annotations

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

RAW = (b"From: a@x.com\r\nTo: me@acme.com\r\nSubject: s\r\n"
       b"Message-ID: <m@x.com>\r\n\r\nbody\r\n")


def _seed():
    return {StreamRef(mailbox="me@acme.com", folder="inbox"): [SeedEmail("M1", RAW)]}


def test_connect_on_report_fires_on_fetch_new() -> None:
    reports = []
    mf = connect("memory", seed=_seed(), on_report=reports.append)
    emails = mf.fetch_new()
    assert len(reports) == 1
    assert reports[0].emitted == 1
    assert len(emails) == 1


def test_connect_on_email_and_on_trace_both_fire() -> None:
    received_emails = []
    received_traces = []
    mf = connect(
        "memory", seed=_seed(),
        on_email=received_emails.append, on_trace=received_traces.append,
    )
    mf.run()
    assert len(received_emails) == 1
    assert len(received_traces) == 1
    assert received_traces[0].disposition.value == "emitted"


def test_connect_on_trace_fires_via_stream() -> None:
    traces = []
    mf = connect("memory", seed=_seed(), on_trace=traces.append)
    emails = list(mf.stream())
    assert len(emails) == 1
    assert len(traces) == 1
    assert traces[0].disposition.value == "emitted"


def test_connect_on_filtered_drop_still_reports_dropped_trace() -> None:
    traces = []
    mf = connect(
        "memory", seed=_seed(), on_filtered="drop", on_trace=traces.append,
        filters=[lambda env: False],  # reject everything
    )
    emails = mf.fetch_new()
    assert emails == []
    assert [t.disposition.value for t in traces] == ["dropped"]
