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


_RFC822 = (
    b"Message-ID: <m1@x>\r\nFrom: a@x.com\r\nTo: ops@acme.com\r\n"
    b"Subject: hello\r\n\r\nbody"
)


def _extract(thread_key: str = "") -> object:
    from mailflow.extract.mime import MimeExtractor

    return MimeExtractor().extract_bytes(
        _RFC822,
        provider="gmail",
        provider_message_id="m1",
        stream_id="ops@acme.com/Inbox",
        watched_mailbox="ops@acme.com",
        thread_key=thread_key,
    )


def test_extract_bytes_default_thread_key_empty() -> None:
    assert _extract().thread_key == ""


def test_extract_bytes_carries_thread_key() -> None:
    assert _extract("t-9").thread_key == "t-9"


# --- spine 3: Pipeline cleaner seam + _extract wiring ---

from mailflow.core.models import CleanEmail
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)


class _MarkCleaner:
    def clean(self, email: CleanEmail) -> CleanEmail:
        email.body_text = "CLEANED"
        return email


def _pipeline(*, cleaner: object | None = None) -> tuple[Pipeline, MemoryEmitter]:
    emit = MemoryEmitter()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", _RFC822)]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=emit,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
        cleaner=cleaner,  # type: ignore[arg-type]
    )
    return pipe, emit


def test_pipeline_accepts_and_applies_cleaner() -> None:
    pipe, emit = _pipeline(cleaner=_MarkCleaner())
    pipe.run_once()
    assert emit.events[0].email.body_text == "CLEANED"


def test_pipeline_extract_passes_thread_key_through() -> None:
    pipe, _ = _pipeline()
    msg = _raw(raw_bytes=_RFC822, thread_key="t-1")
    env = MimeEnvelopeParser().parse_envelope(msg, "acme")
    assert pipe._extract(msg, env).thread_key == "t-1"
