"""REL-5 / INT-4 — restart must NOT re-seed the cursor to the watch's current historyId.

The bug: `bootstrap_watches` seeds the cursor from the Gmail watch's *current* historyId on
every start. On a restart, that historyId is AHEAD of the stored cursor, so the monotonic
`commit_if_ahead` advances the cursor past everything that arrived during downtime -> that mail
is never diffed/fetched -> silently lost, on every restart.

Fix: only seed when no cursor is stored yet (first-ever start). A restart keeps its cursor.
"""

from __future__ import annotations

import pytest

from mailflow.adapters.gmail.bootstrap import bootstrap_watches
from mailflow.adapters.gmail.watch import WatchHandle
from mailflow.core.models import Cursor, StreamRef
from mailflow.stores.memory import InMemoryCursorStore

pytestmark = pytest.mark.reliability


class _FakeWatchManager:
    """Minimal watch manager: ensure_watch returns a handle at a fixed historyId."""

    def __init__(self, history_id: str) -> None:
        self._history_id = history_id

    def ensure_watch(self, stream: StreamRef) -> WatchHandle:
        return WatchHandle(mailbox=stream.mailbox, history_id=self._history_id, expiration="0")


def test_restart_does_not_reseed_existing_cursor() -> None:
    # A prior run left the cursor at historyId 500 (last processed before downtime).
    store = InMemoryCursorStore()
    stream = StreamRef(mailbox="me", folder=None)
    store.commit_if_ahead("acme", stream, Cursor(value="500", order=500))

    # Restart: the watch's CURRENT historyId is 800 (mail 500..800 arrived during downtime).
    bootstrap_watches(
        watch_manager=_FakeWatchManager("800"),
        cursor_store=store, tenant="acme", mailboxes=["me"],
    )

    # The cursor must NOT jump to 800 — that would skip mail 500..800 forever.
    cur = store.get("acme", stream)
    assert cur is not None
    assert cur.order == 500, f"cursor re-seeded to {cur.order}, skipping downtime mail 500..800"


def test_bootstrap_still_seeds_when_no_cursor() -> None:
    # First-ever start: no stored cursor -> seed from the watch historyId (unchanged behavior).
    store = InMemoryCursorStore()
    stream = StreamRef(mailbox="me", folder=None)
    bootstrap_watches(
        watch_manager=_FakeWatchManager("900"),
        cursor_store=store, tenant="acme", mailboxes=["me"],
    )
    cur = store.get("acme", stream)
    assert cur is not None and cur.order == 900
