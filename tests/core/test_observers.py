import logging

from mailflow.core.models import Disposition
from mailflow.core.observability import (
    DecisionTrace, Observers, RunReport, notify_observers,
)

_log = logging.getLogger("mailflow.observability")


def _trace() -> DecisionTrace:
    return DecisionTrace(canonical_id="c1", tenant="t", stream="s",
                         disposition=Disposition.dropped, stage="filter",
                         matched_filter="blacklist", reason="spam.com")


def test_observers_any_reports_whether_a_callback_is_set():
    assert Observers().any() is False
    assert Observers(on_trace=lambda t: None).any() is True


def test_notify_delivers_the_argument():
    seen = []
    notify_observers(seen.append, _trace(), logger=_log)
    assert len(seen) == 1 and seen[0].matched_filter == "blacklist"


def test_notify_swallows_and_logs_a_throwing_callback(caplog):
    def boom(_): raise RuntimeError("consumer bug")
    with caplog.at_level(logging.WARNING, logger="mailflow.observability"):
        notify_observers(boom, _trace(), logger=_log)   # must NOT raise
    assert any("raised" in r.message or r.exc_info for r in caplog.records)


def test_notify_report_delivers_run_report():
    seen = []
    notify_observers(seen.append, RunReport(emitted=3), logger=_log)
    assert seen[0].emitted == 3
