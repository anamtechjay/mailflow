"""Gmail reliability: stale-historyId (404) recovery, sweep, renewal, scheduler.

Pure-logic tests with fakes — no Google SDK, no real sleeps."""

from __future__ import annotations

import threading

from mailflow.adapters.gmail.bootstrap import renew_watches, should_schedule_renew, sweep_once
from mailflow.adapters.gmail.watch import WatchHandle
from mailflow.adapters.gmail.provider import GmailProvider
from mailflow.adapters.gmail.scheduler import IntervalScheduler
from mailflow.adapters.gmail.transport import GmailError, StaleHistoryError
from mailflow.core.models import Cursor, StreamRef
from mailflow.stores.memory import InMemoryCursorStore

STREAM = StreamRef(mailbox="ops@acme.com", folder=None)


# ---- GAP 2: stale-historyId (404) recovery ----

class _StaleClient:
    """history_message_ids raises StaleHistoryError; get_profile gives the current id."""
    def __init__(self, current: str) -> None:
        self.current = current
    def history_message_ids(self, user_id, start, label_id=None):
        raise StaleHistoryError(f"historyId {start} too old")
    def get_profile(self, user_id):
        return {"historyId": self.current}


class _BoomClient:
    def history_message_ids(self, user_id, start, label_id=None):
        raise GmailError(500, "server error")
    def get_profile(self, user_id):
        return {"historyId": "1"}


def test_stale_history_reseeds_cursor_and_yields_nothing() -> None:
    store = InMemoryCursorStore()
    store.commit_if_ahead("acme", STREAM, Cursor(value="100", order=100))  # stale cursor
    provider = GmailProvider(client=_StaleClient("500"), cursor_store=store, tenant="acme")
    out = list(provider.fetch(STREAM, store.get("acme", STREAM)))
    assert out == []                                            # gap skipped
    got = store.get("acme", STREAM)
    assert got is not None and got.order == 500                # re-seeded to current


def test_non_404_error_still_propagates() -> None:
    provider = GmailProvider(client=_BoomClient(), cursor_store=InMemoryCursorStore(), tenant="acme")
    cur = Cursor(value="100", order=100)
    try:
        list(provider.fetch(STREAM, cur))
        assert False, "expected GmailError"
    except GmailError as exc:
        assert exc.status_code == 500


# ---- GAP 3: sweep ----

class _FakeProvider:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, str]] = []
    def submit(self, email_address, history_id):
        self.submitted.append((email_address, str(history_id)))


class _FakePipeline:
    def __init__(self) -> None:
        self.runs = 0
    def run_once(self):
        self.runs += 1
        return None


class _FakeRuntime:
    def __init__(self) -> None:
        self.provider = _FakeProvider()
        self.pipeline = _FakePipeline()


class _ProfileClient:
    def get_profile(self, user_id):
        return {"historyId": "777"}


def test_sweep_submits_each_mailbox_and_runs_once() -> None:
    rt = _FakeRuntime()
    sweep_once(runtime=rt, client=_ProfileClient(), mailboxes=["a@x.com", "b@x.com"])
    assert [m for m, _ in rt.provider.submitted] == ["a@x.com", "b@x.com"]
    assert rt.pipeline.runs == 1                                # one pass diffs all streams


# ---- scheduler (smoke; no timing assertions beyond "it fires") ----

def test_interval_scheduler_fires_and_stops() -> None:
    fired = threading.Event()
    sched = IntervalScheduler()
    sched.every(0.01, fired.set, "t")
    assert fired.wait(2.0) is True                             # fn ran at least once
    sched.stop()


# ---- A1: renewal driver fires the real renew_watches closure through the scheduler ----

class _RecordingWatchManager:
    """Duck-typed stand-in for GmailWatchManager: records each renewed mailbox and
    returns a fresh WatchHandle (mirrors GmailWatchManager.renew_watch)."""
    def __init__(self) -> None:
        self.renewed: list[str] = []

    def renew_watch(self, handle: WatchHandle) -> WatchHandle:
        self.renewed.append(handle.mailbox)
        return WatchHandle(mailbox=handle.mailbox, history_id="999", expiration="renewed")


def test_renewal_driver_fires_renew_for_each_handle_through_scheduler() -> None:
    fired = threading.Event()
    watch_manager = _RecordingWatchManager()
    handles = [
        WatchHandle(mailbox="a@x.com", history_id="1"),
        WatchHandle(mailbox="b@x.com", history_id="2"),
    ]

    def tick() -> None:
        # mirrors run_service's lambda: renew_watches(watch_manager=..., handles=handles)
        renew_watches(watch_manager=watch_manager, handles=handles)
        fired.set()

    sched = IntervalScheduler()
    sched.every(0.01, tick, "gmail-watch-renew")
    try:
        assert fired.wait(2.0) is True                      # the daemon tick ran
    finally:
        sched.stop()
    # both mailboxes re-watched on a tick. Assert on the first two only: the scheduler
    # re-arms every 0.01s, so a slow stall before sched.stop() could let a second tick
    # append more entries — the contract under test is "one tick renews every handle in order".
    assert watch_manager.renewed[:2] == ["a@x.com", "b@x.com"]


# ---- A2: renewal-driver scheduling guard truth table ----

def test_should_schedule_renew_truth_table() -> None:
    H = [WatchHandle(mailbox="a@x.com", history_id="1")]
    # armed only when watch started AND interval positive AND at least one handle
    assert should_schedule_renew(start_watch=True, watch_renew_seconds=86400, handles=H) is True
    assert should_schedule_renew(start_watch=False, watch_renew_seconds=86400, handles=H) is False
    assert should_schedule_renew(start_watch=True, watch_renew_seconds=0, handles=H) is False
    assert should_schedule_renew(start_watch=True, watch_renew_seconds=86400, handles=[]) is False
