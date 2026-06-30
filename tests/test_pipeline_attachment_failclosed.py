"""Per-attachment cap fails closed at the pipeline boundary: an over-cap attachment
dead-letters the whole message exactly once (parallels the B1 message-size guard)."""

from __future__ import annotations

from email.message import EmailMessage

from mailflow.core.models import StreamRef
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

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _raw_with_big_attachment() -> bytes:
    m = EmailMessage()
    m["Message-ID"] = "<big.1@example.com>"
    m["From"] = "alice@partner.com"
    m["To"] = "ops@acme.com"
    m["Subject"] = "huge"
    m.set_content("see attached")
    m.add_attachment(b"A" * 4096, maintype="application", subtype="pdf", filename="big.pdf")
    return m.as_bytes()


def _pipe(extractor: MimeExtractor) -> tuple[Pipeline, MemoryEmitter, MemoryEmitter]:
    emit, dlq = MemoryEmitter(), MemoryEmitter()
    raw = _raw_with_big_attachment()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw)]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=extractor,
        emitter=emit,
        dlq_emitter=dlq,
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )
    return pipe, emit, dlq


def test_over_cap_attachment_dead_letters_once() -> None:
    pipe, emit, dlq = _pipe(MimeExtractor(max_attachment_bytes=64))
    r = pipe.run_once()
    assert r.emitted == 0 and len(emit.events) == 0
    assert r.dead_lettered == 1 and len(dlq.events) == 1


def test_under_cap_attachment_still_emits() -> None:
    pipe, emit, dlq = _pipe(MimeExtractor(max_attachment_bytes=1_000_000))
    r = pipe.run_once()
    assert r.emitted == 1 and r.dead_lettered == 0 and len(emit.events) == 1
