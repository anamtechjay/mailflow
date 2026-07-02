"""Restored from ba69e7b + new metrics seam. RunReport counts dispositions;
dead_lettered is owned solely by add_dead_letter (record() never counts it)."""

from __future__ import annotations

from mailflow.core.models import Disposition
from mailflow.core.observability import DeadLetter, DecisionTrace, RunReport


def test_run_report_counts_and_accumulates() -> None:
    report = RunReport()
    report.record(DecisionTrace(
        canonical_id="c1", tenant="acme", stream="ops@acme.com:Inbox",
        disposition=Disposition.emitted, stage="emit",
    ))
    report.record(DecisionTrace(
        canonical_id="c2", tenant="acme", stream="ops@acme.com:Inbox",
        disposition=Disposition.dropped, stage="filter", matched_filter="blacklist",
    ))
    report.add_dead_letter(
        DeadLetter(canonical_id="c3", reason="oversized", provider_message_id="m3")
    )
    assert report.emitted == 1
    assert report.dropped == 1
    assert report.dead_lettered == 1
    assert len(report.traces) == 2
    assert report.dlq[0].reason == "oversized"


def test_record_does_not_double_count_dead_lettered() -> None:
    # The pipeline calls add_dead_letter() AND record(dead_lettered) as a pair;
    # record() must NOT also count it, or one poison message would count twice.
    report = RunReport()
    report.add_dead_letter(
        DeadLetter(canonical_id="c1", reason="poison", provider_message_id="m1")
    )
    report.record(DecisionTrace(
        canonical_id="c1", tenant="acme", stream="s", stage="dlq",
        disposition=Disposition.dead_lettered, reason="poison",
    ))
    assert report.dead_lettered == 1
    assert len(report.traces) == 1


def test_counters_exposes_disposition_metrics() -> None:
    report = RunReport()
    report.fetched = 5
    report.record(DecisionTrace(
        canonical_id="c1", tenant="t", stream="s",
        disposition=Disposition.emitted, stage="emit",
    ))
    report.add_dead_letter(
        DeadLetter(canonical_id="c2", reason="x", provider_message_id="m2")
    )
    assert report.counters() == {
        "fetched": 5, "emitted": 1, "dropped": 0,
        "duplicate": 0, "dead_lettered": 1,
        "attachments_stripped": 0,
    }
