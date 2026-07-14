"""SQLite state stores — the persistent cursor + dedupe adapters.

The key property is restart-safety: a NEW store instance pointed at the same .db
file must see the prior cursor and prior 'done' claims. The §8 invariants
(monotonic CAS, atomic claim, attempt counting) mirror stores/memory.py exactly.
"""

from __future__ import annotations

import sqlite3

from mailflow.core.models import Cursor, StreamRef
from mailflow.stores.sqlite import SqliteCursorStore, SqliteDedupeStore

TENANT = "acme"
STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


# ---- cursor: monotonic compare-and-set (§8.3) ----

def test_cursor_get_none_when_unset(tmp_path) -> None:
    store = SqliteCursorStore(str(tmp_path / "c.db"))
    assert store.get(TENANT, STREAM) is None


def test_cursor_commit_then_get(tmp_path) -> None:
    store = SqliteCursorStore(str(tmp_path / "c.db"))
    assert store.commit_if_ahead(TENANT, STREAM, Cursor(value="100", order=100)) is True
    got = store.get(TENANT, STREAM)
    assert got is not None and got.value == "100" and got.order == 100


def test_cursor_rejects_stale_or_equal(tmp_path) -> None:
    store = SqliteCursorStore(str(tmp_path / "c.db"))
    store.commit_if_ahead(TENANT, STREAM, Cursor(value="100", order=100))
    assert store.commit_if_ahead(TENANT, STREAM, Cursor(value="100", order=100)) is False
    assert store.commit_if_ahead(TENANT, STREAM, Cursor(value="50", order=50)) is False
    assert store.commit_if_ahead(TENANT, STREAM, Cursor(value="150", order=150)) is True
    got = store.get(TENANT, STREAM)
    assert got is not None and got.order == 150


def test_cursor_persists_across_new_instance(tmp_path) -> None:
    path = str(tmp_path / "c.db")
    SqliteCursorStore(path).commit_if_ahead(TENANT, STREAM, Cursor(value="200", order=200))
    # brand-new instance, same file — restart simulation
    reopened = SqliteCursorStore(path)
    got = reopened.get(TENANT, STREAM)
    assert got is not None and got.order == 200
    # and it still rejects stale after restart
    assert reopened.commit_if_ahead(TENANT, STREAM, Cursor(value="150", order=150)) is False


# ---- dedupe: atomic claim + attempts (§8.2 / §8.4) ----

def test_dedupe_claim_is_exclusive(tmp_path) -> None:
    store = SqliteDedupeStore(str(tmp_path / "d.db"))
    assert store.try_claim("k1", 300) is True
    assert store.try_claim("k1", 300) is False  # already claimed


def test_dedupe_record_attempt_increments(tmp_path) -> None:
    store = SqliteDedupeStore(str(tmp_path / "d.db"))
    store.try_claim("k1", 300)
    assert store.record_attempt("k1") == 1
    assert store.record_attempt("k1") == 2


def test_dedupe_release_frees_undone_claim(tmp_path) -> None:
    store = SqliteDedupeStore(str(tmp_path / "d.db"))
    store.try_claim("k1", 300)
    store.release("k1")
    assert store.try_claim("k1", 300) is True  # freed -> claimable again


def test_dedupe_release_keeps_done_claim(tmp_path) -> None:
    store = SqliteDedupeStore(str(tmp_path / "d.db"))
    store.try_claim("k1", 300)
    store.mark_done("k1", 60)
    store.release("k1")  # must NOT free a done claim
    assert store.try_claim("k1", 300) is False


def test_dedupe_done_persists_across_new_instance(tmp_path) -> None:
    path = str(tmp_path / "d.db")
    first = SqliteDedupeStore(path)
    first.try_claim("k1", 300)
    first.mark_done("k1", 60)
    # restart: a new instance must still see k1 as done (not re-claimable)
    assert SqliteDedupeStore(path).try_claim("k1", 300) is False


# ---- dedupe: TTL purge (mark_done's ttl_seconds now actually expires rows) ----

def test_purge_expired_removes_only_expired_done_claims(tmp_path) -> None:
    clock = {"now": 1_000.0}
    store = SqliteDedupeStore(str(tmp_path / "d.db"), clock=lambda: clock["now"])
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


def test_purge_expired_persists_across_new_instance(tmp_path) -> None:
    path = str(tmp_path / "d.db")
    clock = {"now": 1_000.0}
    first = SqliteDedupeStore(path, clock=lambda: clock["now"])
    first.try_claim("expired", 300)
    first.mark_done("expired", ttl_seconds=60)

    reopened = SqliteDedupeStore(path, clock=lambda: 1_100.0)
    assert reopened.purge_expired() == 1


# ---- dedupe: legacy pre-lease/pre-ttl db migration (finding I1) ----

def test_dedupe_migrates_legacy_claims_table_missing_claimed_column(tmp_path) -> None:
    """A `claims` table created before the lease-expiry feature existed had only
    (key, done, attempts) -- no `claimed` column. Opening it with SqliteDedupeStore
    must migrate the table (adding `claimed` alongside `claimed_at`/`expires_at`)
    rather than crashing on the first try_claim()."""
    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE claims ("
        "key TEXT PRIMARY KEY, "
        "done INTEGER NOT NULL DEFAULT 0, "
        "attempts INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    raw.commit()
    raw.close()

    store = SqliteDedupeStore(path)
    assert store.try_claim("k1", 300) is True

    cols = {row[1] for row in store._conn.execute("PRAGMA table_info(claims)")}
    assert "claimed" in cols
