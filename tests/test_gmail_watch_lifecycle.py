"""Gmail watch lifecycle — the registration + cursor-seeding + renewal path.

Pure-logic tests with a fake client (no Google SDK, no network). Covers the parts of
the live reliability machinery that don't need the `gmail` extra:
  - GmailWatchManager.ensure_watch / renew_watch / stop  (watch.py)
  - bootstrap_watches: seed the cursor with the watch's starting historyId  (bootstrap.py)
  - verify_oauth_scopes: the startup scope-check seam  (live.py)
test_gmail_reliability.py already covers renew_watches / should_schedule_renew / sweep;
this fills in the watch-manager + bootstrap + scope-check gaps.
"""

from __future__ import annotations

from typing import Any

from mailflow.adapters.gmail.bootstrap import bootstrap_watches
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.live import verify_oauth_scopes
from mailflow.adapters.gmail.watch import GmailWatchManager, WatchHandle
from mailflow.core.models import StreamRef
from mailflow.stores.memory import InMemoryCursorStore

GCFG = GmailConfig(
    client_id="cid",
    client_secret_ref="env://SECRET",
    oauth_refresh_token_ref="env://REFRESH",
    mailboxes=["me"],
    label_ids=["INBOX"],
)
PCFG = PubSubConfig(project_id="proj", topic="gmail-notifications", subscription="mailflow")


class FakeGmailClient:
    """Records watch/stop calls and returns a canned users.watch() response."""

    def __init__(self, watch_out: dict[str, Any] | None = None) -> None:
        self.watch_out = watch_out if watch_out is not None else {
            "historyId": "555", "expiration": "1700000000000",
        }
        self.watch_calls: list[tuple[str, str, list[str]]] = []
        self.stop_calls: list[str] = []

    def watch(self, user_id: str, topic_path: str, label_ids: list[str]) -> dict[str, Any]:
        self.watch_calls.append((user_id, topic_path, list(label_ids)))
        return dict(self.watch_out)

    def stop(self, user_id: str) -> None:
        self.stop_calls.append(user_id)


def _mgr(client: Any) -> GmailWatchManager:
    return GmailWatchManager(client=client, config=GCFG, pubsub=PCFG)


# ---- GmailWatchManager (watch.py) ----

def test_ensure_watch_maps_client_output_to_handle() -> None:
    c = FakeGmailClient({"historyId": "555", "expiration": "1700000000000"})
    handle = _mgr(c).ensure_watch(StreamRef(mailbox="me", folder=None))
    assert handle == WatchHandle(mailbox="me", history_id="555", expiration="1700000000000")
    # watch() is called with the resolved topic_path + the config's label_ids
    assert c.watch_calls == [("me", PCFG.topic_path, ["INBOX"])]


def test_ensure_watch_tolerates_missing_fields() -> None:
    handle = _mgr(FakeGmailClient({})).ensure_watch(StreamRef(mailbox="me", folder=None))
    assert handle.history_id == ""
    assert handle.expiration == ""


def test_renew_watch_rewatches_the_same_mailbox() -> None:
    # Gmail has no renew endpoint — renewal is just calling watch() again.
    c = FakeGmailClient({"historyId": "777"})
    renewed = _mgr(c).renew_watch(WatchHandle(mailbox="me", history_id="100"))
    assert renewed.history_id == "777"
    assert c.watch_calls == [("me", PCFG.topic_path, ["INBOX"])]


def test_stop_calls_client_stop() -> None:
    c = FakeGmailClient()
    _mgr(c).stop(WatchHandle(mailbox="me", history_id="1"))
    assert c.stop_calls == ["me"]


# ---- bootstrap_watches (bootstrap.py) ----

def test_bootstrap_registers_watch_and_seeds_cursor() -> None:
    c = FakeGmailClient({"historyId": "900"})
    store = InMemoryCursorStore()
    handles = bootstrap_watches(
        watch_manager=_mgr(c), cursor_store=store, tenant="acme", mailboxes=["me"],
    )
    assert len(handles) == 1 and handles[0].history_id == "900"
    cur = store.get("acme", StreamRef(mailbox="me", folder=None))
    assert cur is not None and cur.value == "900" and cur.order == 900


def test_bootstrap_seeds_each_mailbox_independently() -> None:
    c = FakeGmailClient({"historyId": "42"})
    store = InMemoryCursorStore()
    handles = bootstrap_watches(
        watch_manager=_mgr(c), cursor_store=store, tenant="acme",
        mailboxes=["a@x.com", "b@x.com"],
    )
    assert len(handles) == 2
    assert len(c.watch_calls) == 2
    for mbx in ("a@x.com", "b@x.com"):
        cur = store.get("acme", StreamRef(mailbox=mbx, folder=None))
        assert cur is not None and cur.order == 42


def test_bootstrap_does_not_seed_cursor_without_history_id() -> None:
    c = FakeGmailClient({})  # empty historyId -> nothing to seed from
    store = InMemoryCursorStore()
    handles = bootstrap_watches(
        watch_manager=_mgr(c), cursor_store=store, tenant="acme", mailboxes=["me"],
    )
    assert handles[0].history_id == ""
    assert store.get("acme", StreamRef(mailbox="me", folder=None)) is None


# ---- verify_oauth_scopes (live.py) ----

class _FakeToken:
    def __init__(self) -> None:
        self.checked: list[str] | None = None

    def verify_scopes(self, required: list[str]) -> None:
        self.checked = list(required)


def test_verify_oauth_scopes_runs_when_enabled() -> None:
    tok = _FakeToken()
    verify_oauth_scopes(tok, ["scope.a", "scope.b"], enabled=True)
    assert tok.checked == ["scope.a", "scope.b"]


def test_verify_oauth_scopes_skipped_when_disabled() -> None:
    tok = _FakeToken()
    verify_oauth_scopes(tok, ["scope.a"], enabled=False)
    assert tok.checked is None
