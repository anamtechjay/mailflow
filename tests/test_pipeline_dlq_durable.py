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


class _FailingDeadLetterStore:
    """A DeadLetterStore whose put() always raises, to prove the durable write happens
    BEFORE the irreversible mark_done (so a store failure leaves the message recoverable)."""

    def __init__(self) -> None:
        self.calls = 0

    def put(self, record: Any) -> None:
        self.calls += 1
        raise RuntimeError("dlq store down")

    def list_pending(self, *, limit: int | None = None) -> list[Any]:
        return []

    def delete(self, record_id: str) -> None:
        return None


def test_durable_write_precedes_mark_done_so_failure_is_recoverable() -> None:
    import pytest

    dedupe = InMemoryDedupeStore()
    store = _FailingDeadLetterStore()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=RaisingExtractor(PermanentError("nope")),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=dedupe,
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
        dlq_store=store,
    )
    # The failing durable write propagates...
    with pytest.raises(RuntimeError):
        pipe.run_once()
    assert store.calls == 1

    # ...and crucially the message was NOT marked done. Real InMemoryDedupeStore semantics:
    # `try_claim(key, lease)` returns False while the key is present (it was added by the
    # claim at the start of _process), so it cannot directly prove not-done here. Instead we
    # use `release`, which models lease-expiry / redelivery: it drops a claim ONLY when it is
    # NOT done. A recoverable (not-done) message therefore becomes reclaimable...
    key = "acme|ops@acme.com|m1"
    dedupe.release(key)
    assert dedupe.try_claim(key, 300) is True  # reclaimable -> reprocessable -> re-dead-letterable
    dedupe.release(key)  # let go again so the real reprocessing run below can re-claim it

    # End-to-end proof: with a working store wired in, the reclaimed message dead-letters and
    # lands durably (it was never lost despite the earlier store outage).
    good = InMemoryDeadLetterStore()
    pipe2 = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=RaisingExtractor(PermanentError("nope")),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=dedupe,
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
        dlq_store=good,
    )
    report = pipe2.run_once()
    assert report.dead_lettered == 1
    assert len(good.list_pending()) == 1
