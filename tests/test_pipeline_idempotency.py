"""A4: the emitted EmailEvent carries the idempotency_key (tenant|mailbox|provider_message_id)."""

from __future__ import annotations

from mailflow.core.identity import idempotency_key
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.core.models import StreamRef
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")
RAW = (b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\n"
       b"To: ops@acme.com\r\nSubject: hi\r\n\r\nbody")


def test_emitted_event_carries_idempotency_key() -> None:
    emit = MemoryEmitter()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("pmid-123", RAW)]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=emit, dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )
    pipe.run_once()
    assert len(emit.events) == 1
    expected = idempotency_key("acme", "ops@acme.com", "pmid-123")
    assert expected == "acme|ops@acme.com|pmid-123"
    assert emit.events[0].idempotency_key == expected
