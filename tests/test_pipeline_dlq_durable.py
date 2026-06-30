"""A dead-letter still counts exactly once (frozen DLQ contract) AND, when a
DeadLetterStore is wired, writes a durable replayable record carrying the raw bytes,
error class, and attempt count."""

from __future__ import annotations

from typing import Any

from mailflow.core.errors import PermanentError
from mailflow.core.models import Envelope, RawMessage, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDeadLetterStore,
    InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def raw(mid: str = "m1") -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: a@partner.com\r\n"
            f"To: ops@acme.com\r\nSubject: hi\r\n\r\nbody").encode()


class RaisingExtractor:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def extract(self, msg: RawMessage, env: Envelope) -> Any:
        raise self.exc


def test_dead_letter_writes_durable_record_and_counts_once() -> None:
    dlq_store = InMemoryDeadLetterStore()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=RaisingExtractor(PermanentError("nope")),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
        dlq_store=dlq_store,
    )
    r = pipe.run_once()
    assert r.dead_lettered == 1                      # frozen contract: counted once
    records = dlq_store.list_pending()
    assert len(records) == 1
    rec = records[0]
    assert rec.error_class == "PermanentError"
    assert rec.attempts >= 1
    assert rec.raw_b64 != ""                         # raw captured for replay
    assert rec.provider_message_id == "m1"
    assert rec.record_id == "acme|ops@acme.com|m1"
