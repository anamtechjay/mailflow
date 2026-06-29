"""Phase 1a spine — behaviour-free cross-cutting seams for A7 (thread_key) and A6 (cleaner).

These make the seams compile/green so the parallel lanes can fill in behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mailflow.core.models import Cursor, RawMessage, StreamRef

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _raw(**kw: object) -> RawMessage:
    base: dict[str, object] = dict(
        provider="gmail",
        provider_message_id="m1",
        stream=STREAM,
        size_bytes=10,
        received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        cursor=Cursor(value="100", order=100),
    )
    base.update(kw)
    return RawMessage(**base)  # type: ignore[arg-type]


def test_raw_message_has_thread_key_default_empty() -> None:
    assert _raw().thread_key == ""


def test_raw_message_thread_key_round_trips() -> None:
    assert _raw(thread_key="t-123").thread_key == "t-123"
