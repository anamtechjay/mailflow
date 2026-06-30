"""redrive() rebuilds each durable dead-letter into its original RawMessage and re-runs
it through a fresh, redrive-scoped pipeline. A record that now reaches a non-DLQ terminal
disposition is deleted from the store; one that fails again is kept."""

from __future__ import annotations

from typing import Any

from mailflow.core.errors import PermanentError
from mailflow.core.models import Envelope, RawMessage, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.redrive import rebuild_raw_message, redrive
from mailflow.core.observability import DeadLetterRecord
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


def _poison_run_into(dlq_store: InMemoryDeadLetterStore, exc: Exception) -> None:
    Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(), filters=FilterChain([]),
        extractor=RaisingExtractor(exc),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="acme"),
        dlq_store=dlq_store,
    ).run_once()


def test_rebuild_raw_message_restores_bytes_and_stream() -> None:
    dlq_store = InMemoryDeadLetterStore()
    _poison_run_into(dlq_store, PermanentError("synthetic"))
    rec = dlq_store.list_pending()[0]
    msg = rebuild_raw_message(rec)
    assert msg.raw_bytes == raw("m1")
    assert msg.stream == STREAM
    assert msg.provider_message_id == "m1"


def test_redrive_resubmits_and_clears_on_success() -> None:
    dlq_store = InMemoryDeadLetterStore()
    _poison_run_into(dlq_store, PermanentError("synthetic"))
    assert len(dlq_store.list_pending()) == 1

    out_emit, out_dlq = MemoryEmitter(), MemoryEmitter()
    report = redrive(
        store=dlq_store, emitter=out_emit, dlq_emitter=out_dlq,
        blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="acme"),
    )
    assert report.examined == 1 and report.resubmitted == 1
    assert report.still_dead_lettered == 0
    assert len(out_emit.events) == 1            # re-emitted with a real MimeExtractor
    assert dlq_store.list_pending() == []       # cleared after success


def test_redrive_keeps_record_when_it_fails_again() -> None:
    dlq_store = InMemoryDeadLetterStore()
    _poison_run_into(dlq_store, PermanentError("synthetic"))
    report = redrive(
        store=dlq_store, emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="acme"),
        extractor=RaisingExtractor(PermanentError("still broken")),
    )
    assert report.examined == 1 and report.resubmitted == 0
    assert report.still_dead_lettered == 1
    assert len(dlq_store.list_pending()) == 1


def test_redrive_empty_store_is_a_noop() -> None:
    report = redrive(
        store=InMemoryDeadLetterStore(), emitter=MemoryEmitter(),
        dlq_emitter=MemoryEmitter(), blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )
    assert report.examined == 0 and report.resubmitted == 0
