"""connect() must wire a REAL, durable DeadLetterStore (per state=), not silently
discard dead-lettered messages into a throwaway MemoryEmitter nobody can read back."""

from __future__ import annotations

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail


def test_memory_connect_dead_letters_are_durable(tmp_path) -> None:
    stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
    huge_raw = (
        b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: hi\r\n\r\n"
        + b"x" * 50_000_001  # forces size > default max_message_bytes (50_000_000), no override needed
    )
    mf = connect(
        "memory", seed={stream: [SeedEmail("m1", huge_raw)]}, tenant="acme",
        state=f"sqlite:///{tmp_path}/mf.db",
    )
    mf.fetch_new()

    from mailflow.stores.sqlite import SqliteDeadLetterStore
    dlq = SqliteDeadLetterStore(str(tmp_path / "mf.db"))
    pending = dlq.list_pending()
    assert len(pending) == 1
    assert pending[0].reason.startswith("oversized")
