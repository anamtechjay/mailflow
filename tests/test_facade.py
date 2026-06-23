"""connect() facade — the friendly front door over the engine."""

from __future__ import annotations

from mailflow import connect
from mailflow.core.models import CleanEmail, StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.sqlite import SqliteCursorStore

RAW = (
    b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\n"
    b"To: ops@acme.com\r\nSubject: hello\r\n\r\nbody"
)
STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _seed() -> dict[StreamRef, list[SeedEmail]]:
    return {STREAM: [SeedEmail("m1", RAW)]}


def test_connect_memory_fetch_new_yields_clean_email() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme")
    emails = mf.fetch_new()
    assert len(emails) == 1
    assert isinstance(emails[0], CleanEmail)
    assert emails[0].subject == "hello"
    assert emails[0].canonical_id  # always present


def test_connect_memory_stream_yields_clean_email() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme")
    got = list(mf.stream())  # memory stream is finite (drains the seed)
    assert [e.subject for e in got] == ["hello"]


def test_connect_memory_on_email_callback() -> None:
    seen: list[CleanEmail] = []
    mf = connect("memory", seed=_seed(), tenant="acme", on_email=seen.append)
    mf.run()
    assert len(seen) == 1
    assert seen[0].subject == "hello"


def test_connect_state_sqlite_selects_persistent_store(tmp_path) -> None:
    mf = connect("memory", seed=_seed(), tenant="acme", state=f"sqlite:///{tmp_path}/mf.db")
    assert isinstance(mf.cursor_store, SqliteCursorStore)
