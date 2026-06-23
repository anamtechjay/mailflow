"""Output emitters — the shapes the library hands CleanEmails to the app."""

from __future__ import annotations

from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail
from mailflow.emit.callback import CallbackEmitter, QueueEmitter


def _event(cid: str, subject: str = "hi") -> EmailEvent:
    email = CleanEmail(
        canonical_id=cid, provider="memory", provider_message_id=cid,
        provider_stream_id="s", subject=subject, schema_version=SCHEMA_VERSION,
    )
    return EmailEvent(schema_version=SCHEMA_VERSION, tenant="t", ordering_key="ok", email=email)


def test_callback_emitter_calls_fn_with_clean_email() -> None:
    seen: list[CleanEmail] = []
    emitter = CallbackEmitter(seen.append)
    emitter.emit(_event("m1", "subject-1"))
    assert len(seen) == 1
    assert seen[0].canonical_id == "m1" and seen[0].subject == "subject-1"


def test_queue_emitter_drains_fifo() -> None:
    emitter = QueueEmitter()
    emitter.emit(_event("m1"))
    emitter.emit(_event("m2"))
    out = emitter.drain_nowait()
    assert [e.canonical_id for e in out] == ["m1", "m2"]
