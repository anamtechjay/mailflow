"""DeadLetterStore adapters: put is idempotent by record_id, list_pending is ordered +
limitable, delete removes, and the sqlite store survives a reopen (durability)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mailflow.core.observability import DeadLetterRecord
from mailflow.stores.memory import InMemoryDeadLetterStore
from mailflow.stores.sqlite import SqliteDeadLetterStore


def _record(record_id: str, *, reason: str = "boom") -> DeadLetterRecord:
    now = datetime(2026, 6, 30, tzinfo=timezone.utc)
    return DeadLetterRecord(
        record_id=record_id, tenant="acme", provider="memory",
        provider_message_id=record_id, mailbox="ops@acme.com", folder="Inbox",
        canonical_id=f"<{record_id}@x>", reason=reason, error_class="PermanentError",
        attempts=1, size_bytes=5, thread_key="t", cursor_value="c", cursor_order=1,
        received_at=now, dead_lettered_at=now, raw_b64="aGk=",
    )


def test_inmemory_put_list_delete() -> None:
    store = InMemoryDeadLetterStore()
    store.put(_record("k1"))
    store.put(_record("k2"))
    assert [r.record_id for r in store.list_pending()] == ["k1", "k2"]
    assert [r.record_id for r in store.list_pending(limit=1)] == ["k1"]
    store.delete("k1")
    assert [r.record_id for r in store.list_pending()] == ["k2"]


def test_inmemory_put_idempotent_by_record_id() -> None:
    store = InMemoryDeadLetterStore()
    store.put(_record("k1", reason="a"))
    store.put(_record("k1", reason="b"))
    pending = store.list_pending()
    assert len(pending) == 1 and pending[0].reason == "b"


def test_sqlite_persists_across_reopen(tmp_path: Path) -> None:
    db = str(tmp_path / "dlq.db")
    SqliteDeadLetterStore(db).put(_record("k1"))
    reopened = SqliteDeadLetterStore(db)
    pending = reopened.list_pending()
    assert [r.record_id for r in pending] == ["k1"]
    assert pending[0].raw_b64 == "aGk="
    reopened.delete("k1")
    assert SqliteDeadLetterStore(db).list_pending() == []


def test_sqlite_put_idempotent_by_record_id(tmp_path: Path) -> None:
    db = str(tmp_path / "dlq.db")
    store = SqliteDeadLetterStore(db)
    store.put(_record("k1", reason="a"))
    store.put(_record("k1", reason="b"))
    pending = store.list_pending()
    assert len(pending) == 1 and pending[0].reason == "b"
