"""purge_expired(): mark_done(key, ttl_seconds) must actually expire -- until this test,
ttl_seconds was accepted and silently discarded, so claims accumulated forever."""

from __future__ import annotations

import pytest

from mailflow.stores.memory import InMemoryDedupeStore


def test_purge_expired_removes_only_expired_done_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    # mark_done() stamps expires_at from the real wall clock (time.time()), so the
    # clock must be pinned here -- otherwise these fixed epoch offsets (1_000.0 etc.)
    # would never align with "now" on a real machine.
    clock = {"now": 1_000.0}
    monkeypatch.setattr("mailflow.stores.memory.time.time", lambda: clock["now"])

    store = InMemoryDedupeStore()
    store.try_claim("expired", 300)
    store.mark_done("expired", ttl_seconds=60)   # expires at t=1060
    store.try_claim("fresh", 300)
    store.mark_done("fresh", ttl_seconds=600)    # expires at t=1600
    store.try_claim("not-done", 300)             # never marked done -- must survive

    removed = store.purge_expired(now=1_100.0)   # past "expired"'s ttl, before "fresh"'s

    assert removed == 1
    assert store.try_claim("expired", 300) is True   # gone -> claimable again
    assert store.try_claim("fresh", 300) is False     # still done, not yet expired
    assert store.try_claim("not-done", 300) is False  # untouched, still claimed
