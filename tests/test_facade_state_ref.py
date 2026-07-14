"""connect(state_ref=...) resolves the state= DSN via SecretProvider at connect time,
so a Postgres password never has to be embedded literally in application code."""

from __future__ import annotations

import os

import pytest

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail


def test_state_ref_resolves_via_secret_provider(tmp_path, monkeypatch) -> None:
    dsn = f"sqlite:///{tmp_path}/mf.db"
    monkeypatch.setenv("MAILFLOW_TEST_STATE_DSN", dsn)

    stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
    raw = b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: hi\r\n\r\nhello"
    mf = connect(
        "memory", seed={stream: [SeedEmail("m1", raw)]}, tenant="acme",
        state_ref="env://MAILFLOW_TEST_STATE_DSN",
    )
    mf.fetch_new()

    from mailflow.stores.sqlite import SqliteCursorStore

    cursor_store = SqliteCursorStore(str(tmp_path / "mf.db"))
    assert cursor_store.get("acme", stream) is not None  # proves sqlite state was actually used


def test_state_ref_takes_precedence_over_state() -> None:
    # state="memory" (the default) would be silently used if state_ref were ignored --
    # this proves state_ref, when given, wins.
    with pytest.raises(KeyError, match="not set"):
        connect(
            "memory", seed={}, tenant="acme", state="memory",
            state_ref="env://__MAILFLOW_NONEXISTENT_REF__",
        )
