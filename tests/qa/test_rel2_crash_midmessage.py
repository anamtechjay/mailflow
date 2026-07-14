"""REL-2 / DEP-7 — a claim held by a crashed worker must become reclaimable after
its lease elapses; and must NOT be reclaimable before then.

The bug this fixes: `SqliteDedupeStore.try_claim` recorded `claimed=1` but never a
lease timestamp, and reclaim was gated on `claimed=0`. So a worker that crashed
between `try_claim` and `mark_done` left the row `claimed=1, done=0` FOREVER --
`try_claim` returned False on every retry/restart, the in-flight message was never
reprocessed, and (once the cursor advanced past it on a sibling message) it was
silently lost. There was no lease honoring in any shipped store.

Fix: `try_claim` stamps `claimed_at` from an injectable wall clock; a claimed-but-
not-done row is reclaimable once `now - claimed_at >= lease_seconds`.
"""

from __future__ import annotations

import pytest

from mailflow.stores.sqlite import SqliteDedupeStore

from tests._harness.reliability import FakeClock, crash_between_claim_and_done

pytestmark = pytest.mark.reliability


def test_crashed_claim_not_reclaimable_before_lease(tmp_path):
    clock = FakeClock(start=1000.0)
    db = str(tmp_path / "mf.db")
    store = SqliteDedupeStore(db_path=db, clock=clock)

    # Worker claims + records an attempt, then "crashes" (never mark_done/release).
    crash_between_claim_and_done(store, "k1", lease_seconds=300)

    # A restart handle over the SAME db, only 299s later — still inside the lease.
    clock.advance(299)
    restarted = SqliteDedupeStore(db_path=db, clock=clock)
    assert restarted.try_claim("k1", 300) is False  # still held — must not double-process


def test_crashed_claim_reclaimable_after_lease(tmp_path):
    clock = FakeClock(start=1000.0)
    db = str(tmp_path / "mf.db")
    store = SqliteDedupeStore(db_path=db, clock=clock)

    crash_between_claim_and_done(store, "k1", lease_seconds=300)

    # 301s later the lease has elapsed → a restart may reclaim and reprocess.
    clock.advance(301)
    restarted = SqliteDedupeStore(db_path=db, clock=clock)
    assert restarted.try_claim("k1", 300) is True

    # The attempts counter from before the crash is preserved (REL-3 contract holds).
    assert restarted.record_attempt("k1") == 2


def test_done_claim_never_reclaimable_even_after_lease(tmp_path):
    clock = FakeClock(start=1000.0)
    db = str(tmp_path / "mf.db")
    store = SqliteDedupeStore(db_path=db, clock=clock)

    assert store.try_claim("k1", 300) is True
    store.mark_done("k1", ttl_seconds=60)

    clock.advance(10_000)  # long past any lease
    assert store.try_claim("k1", 300) is False  # done stays done — no reprocessing
