"""SQLite state stores — the persistent cursor + dedupe adapters.

The key property is restart-safety: a NEW store instance pointed at the same .db
file must see the prior cursor and prior 'done' claims. The §8 invariants
(monotonic CAS, atomic claim, attempt counting) mirror stores/memory.py exactly.
"""

from __future__ import annotations

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
