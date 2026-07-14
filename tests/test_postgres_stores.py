"""PostgreSQL state stores — the restart-safe, multi-process-safe cursor + dedupe
adapters. Mirrors tests/test_sqlite_stores.py's cases exactly (same §8 invariants),
plus a real-concurrency test that SQLite's single-connection+lock design can't prove:
two independent connections racing try_claim() on the same key.

Requires a real Postgres reachable at MAILFLOW_TEST_POSTGRES_DSN — skipped otherwise
(this is deliberately a `live` test: Postgres's own transaction semantics are the thing
under test, not something a fake can stand in for).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg
import pytest

from mailflow.core.models import Cursor, StreamRef

DSN = os.environ.get("MAILFLOW_TEST_POSTGRES_DSN", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not DSN, reason="MAILFLOW_TEST_POSTGRES_DSN not set"),
]

TENANT = "acme"
STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")
_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Each test gets empty tables (drop -> next store recreates them), regardless of
    which prior test ran. Runs even before the first store exists."""
    with psycopg.connect(DSN) as conn:
        conn.execute("DROP TABLE IF EXISTS cursors, claims, dead_letters")
        conn.commit()
    yield


# ---- cursor: monotonic compare-and-set (§8.3) ----

def test_cursor_get_none_when_unset() -> None:
    from mailflow.stores.postgres import PostgresCursorStore

    store = PostgresCursorStore(DSN)
    assert store.get(TENANT, STREAM) is None


def test_cursor_commit_then_get() -> None:
    from mailflow.stores.postgres import PostgresCursorStore

    store = PostgresCursorStore(DSN)
    assert store.commit_if_ahead(TENANT, STREAM, Cursor(value="100", order=100)) is True
    got = store.get(TENANT, STREAM)
    assert got is not None and got.value == "100" and got.order == 100


def test_cursor_rejects_stale_or_equal() -> None:
    from mailflow.stores.postgres import PostgresCursorStore

    store = PostgresCursorStore(DSN)
    store.commit_if_ahead(TENANT, STREAM, Cursor(value="100", order=100))
    assert store.commit_if_ahead(TENANT, STREAM, Cursor(value="100", order=100)) is False
    assert store.commit_if_ahead(TENANT, STREAM, Cursor(value="50", order=50)) is False
    assert store.commit_if_ahead(TENANT, STREAM, Cursor(value="150", order=150)) is True
    got = store.get(TENANT, STREAM)
    assert got is not None and got.order == 150


def test_cursor_persists_across_new_instance() -> None:
    from mailflow.stores.postgres import PostgresCursorStore

    PostgresCursorStore(DSN).commit_if_ahead(TENANT, STREAM, Cursor(value="200", order=200))
    reopened = PostgresCursorStore(DSN)  # brand-new instance/connection — restart simulation
    got = reopened.get(TENANT, STREAM)
    assert got is not None and got.order == 200
    assert reopened.commit_if_ahead(TENANT, STREAM, Cursor(value="150", order=150)) is False


# ---- dedupe: atomic claim + attempts (§8.2 / §8.4) ----

def test_dedupe_claim_is_exclusive() -> None:
    from mailflow.stores.postgres import PostgresDedupeStore

    store = PostgresDedupeStore(DSN)
    assert store.try_claim("k1", 300) is True
    assert store.try_claim("k1", 300) is False  # already claimed


def test_dedupe_record_attempt_increments() -> None:
    from mailflow.stores.postgres import PostgresDedupeStore

    store = PostgresDedupeStore(DSN)
    store.try_claim("k1", 300)
    assert store.record_attempt("k1") == 1
    assert store.record_attempt("k1") == 2


def test_dedupe_release_frees_undone_claim() -> None:
    from mailflow.stores.postgres import PostgresDedupeStore

    store = PostgresDedupeStore(DSN)
    store.try_claim("k1", 300)
    store.release("k1")
    assert store.try_claim("k1", 300) is True  # freed -> claimable again


def test_dedupe_release_keeps_done_claim() -> None:
    from mailflow.stores.postgres import PostgresDedupeStore

    store = PostgresDedupeStore(DSN)
    store.try_claim("k1", 300)
    store.mark_done("k1", 60)
    store.release("k1")  # must NOT free a done claim
    assert store.try_claim("k1", 300) is False


def test_dedupe_done_persists_across_new_instance() -> None:
    from mailflow.stores.postgres import PostgresDedupeStore

    first = PostgresDedupeStore(DSN)
    first.try_claim("k1", 300)
    first.mark_done("k1", 60)
    assert PostgresDedupeStore(DSN).try_claim("k1", 300) is False


def test_dedupe_release_keeps_lifetime_attempt_count() -> None:
    """REL-3 contract (same as the sqlite/memory stores): attempts survive a
    release+re-claim cycle -- it's a lifetime counter, not per-claim."""
    from mailflow.stores.postgres import PostgresDedupeStore

    store = PostgresDedupeStore(DSN)
    store.try_claim("k1", 300)
    store.record_attempt("k1")
    store.record_attempt("k1")
    store.release("k1")
    store.try_claim("k1", 300)
    assert store.record_attempt("k1") == 3  # not reset to 1


def test_dedupe_claim_reclaimable_after_lease_expires() -> None:
    """REL-2: a claimed-but-not-done row becomes reclaimable once its lease elapses
    (worker crashed mid-message and never released)."""
    from mailflow.stores.postgres import PostgresDedupeStore

    clock = {"now": 1_000.0}
    store = PostgresDedupeStore(DSN, clock=lambda: clock["now"])
    assert store.try_claim("k1", 10) is True
    assert store.try_claim("k1", 10) is False  # still leased
    clock["now"] += 11
    assert store.try_claim("k1", 10) is True  # lease expired -> reclaimable


def test_dedupe_claim_race_across_two_real_connections() -> None:
    """The reason to reach for Postgres over SQLite: two INDEPENDENT connections
    (simulating two separate worker processes) racing try_claim on the same key --
    exactly one must win. SQLite's single-connection+threading.Lock design can't
    prove this; here it's Postgres's own transaction semantics doing the work."""
    from mailflow.stores.postgres import PostgresDedupeStore

    store_a = PostgresDedupeStore(DSN)
    store_b = PostgresDedupeStore(DSN)
    results = [store_a.try_claim("shared-key", 300), store_b.try_claim("shared-key", 300)]
    assert sorted(results) == [False, True]  # exactly one winner


# ---- dead letter: durable, replayable (spec §A2 redrive) ----

def test_deadletter_put_then_list_pending() -> None:
    from mailflow.core.observability import DeadLetterRecord
    from mailflow.stores.postgres import PostgresDeadLetterStore

    store = PostgresDeadLetterStore(DSN)
    record = DeadLetterRecord(
        record_id="r1", tenant=TENANT, provider="graph", provider_message_id="m1",
        mailbox="ops@acme.com", folder="Inbox", canonical_id="c1", reason="poison",
        error_class="ValueError", attempts=1, size_bytes=10, thread_key="",
        cursor_value="1", cursor_order=1,
        received_at=_TS, dead_lettered_at=_TS,
    )
    store.put(record)
    pending = store.list_pending()
    assert len(pending) == 1 and pending[0].record_id == "r1"


def test_deadletter_delete_removes_record() -> None:
    from mailflow.core.observability import DeadLetterRecord
    from mailflow.stores.postgres import PostgresDeadLetterStore

    store = PostgresDeadLetterStore(DSN)
    record = DeadLetterRecord(
        record_id="r1", tenant=TENANT, provider="graph", provider_message_id="m1",
        mailbox="ops@acme.com", folder="Inbox", canonical_id="c1", reason="poison",
        error_class="ValueError", attempts=1, size_bytes=10, thread_key="",
        cursor_value="1", cursor_order=1,
        received_at=_TS, dead_lettered_at=_TS,
    )
    store.put(record)
    store.delete("r1")
    assert store.list_pending() == []


def test_deadletter_persists_across_new_instance() -> None:
    from mailflow.core.observability import DeadLetterRecord
    from mailflow.stores.postgres import PostgresDeadLetterStore

    record = DeadLetterRecord(
        record_id="r1", tenant=TENANT, provider="graph", provider_message_id="m1",
        mailbox="ops@acme.com", folder="Inbox", canonical_id="c1", reason="poison",
        error_class="ValueError", attempts=1, size_bytes=10, thread_key="",
        cursor_value="1", cursor_order=1,
        received_at=_TS, dead_lettered_at=_TS,
    )
    PostgresDeadLetterStore(DSN).put(record)
    reopened = PostgresDeadLetterStore(DSN)
    assert len(reopened.list_pending()) == 1


# ---- dedupe: TTL purge (spec §8.5 / DEP-7 follow-on) ----

def test_purge_expired_removes_only_expired_done_claims() -> None:
    from mailflow.stores.postgres import PostgresDedupeStore

    clock = {"now": 1_000.0}
    store = PostgresDedupeStore(DSN, clock=lambda: clock["now"])
    store.try_claim("expired", 300)
    store.mark_done("expired", ttl_seconds=60)   # expires at t=1060
    store.try_claim("fresh", 300)
    store.mark_done("fresh", ttl_seconds=600)    # expires at t=1600
    store.try_claim("not-done", 300)

    clock["now"] = 1_100.0
    removed = store.purge_expired()

    assert removed == 1
    assert store.try_claim("expired", 300) is True
    assert store.try_claim("fresh", 300) is False
    assert store.try_claim("not-done", 300) is False


def test_purge_expired_persists_across_new_instance() -> None:
    from mailflow.stores.postgres import PostgresDedupeStore

    clock = {"now": 1_000.0}
    first = PostgresDedupeStore(DSN, clock=lambda: clock["now"])
    first.try_claim("expired", 300)
    first.mark_done("expired", ttl_seconds=60)

    reopened = PostgresDedupeStore(DSN, clock=lambda: 1_100.0)
    assert reopened.purge_expired() == 1


# ---- startup connect retry (Task 10) ----

def test_cursor_store_retries_transient_connect_failure(monkeypatch) -> None:
    import psycopg

    from mailflow.stores.postgres import PostgresCursorStore

    real_connect = psycopg.connect
    calls = {"n": 0}

    def flaky_connect(dsn: str, **kwargs: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 2:
            raise psycopg.OperationalError("transient")
        return real_connect(dsn, **kwargs)

    monkeypatch.setattr(psycopg, "connect", flaky_connect)
    store = PostgresCursorStore(DSN)  # must retry once internally, not raise
    assert calls["n"] == 2
    assert store.get(TENANT, STREAM) is None
