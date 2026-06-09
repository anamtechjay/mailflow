from mailflow.core.models import Disposition
from mailflow.core.observability import DeadLetter, DecisionTrace, RunReport


def test_run_report_counts_and_accumulates():
    report = RunReport()
    report.record(DecisionTrace(
        canonical_id="c1", tenant="acme", stream="ops@acme.com:Inbox",
        disposition=Disposition.emitted, stage="emit",
    ))
    report.record(DecisionTrace(
        canonical_id="c2", tenant="acme", stream="ops@acme.com:Inbox",
        disposition=Disposition.dropped, stage="filter", matched_filter="blacklist",
    ))
    report.add_dead_letter(DeadLetter(canonical_id="c3", reason="oversized", provider_message_id="m3"))
    assert report.emitted == 1
    assert report.dropped == 1
    assert report.dead_lettered == 1
    assert len(report.traces) == 2
    assert report.dlq[0].reason == "oversized"
