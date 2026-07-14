"""F12 — DLQ / redrive (seam). See docs/qa-partA-coverage.md.

`build_memory_pipeline` (tests/_harness/fakes.py) does not expose a `dlq_store`
knob, so the tests that need a durable `DeadLetterRecord` (carries the raw bytes,
overwrite-by-record_id) build a `Pipeline` directly here, mirroring
`build_memory_pipeline`'s wiring plus a `DeadLetterStore`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mailflow.core.models import Cursor, RawMessage, StreamRef
from mailflow.core.observability import Observers, RunReport
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDeadLetterStore,
    InMemoryDedupeStore,
)

TENANT = "acme"
S = StreamRef(mailbox="ops@acme.com", folder="inbox")

# A message with one attachment whose base64 payload is corrupted ("poison"):
# it parses as valid MIME, but decoding the attachment raises
# AttachmentUnreadableError (a PermanentError) -> dead-lettered, never delivered.
POISON_RAW = (
    b"From: a@partner.com\r\n"
    b"To: ops@acme.com\r\n"
    b"Subject: poison\r\n"
    b"Message-ID: <poison1@partner.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="BOUND"\r\n'
    b"\r\n"
    b"--BOUND\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"hello\r\n"
    b"--BOUND\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b'Content-Disposition: attachment; filename="bad.bin"\r\n'
    b"\r\n"
    b"!!!not-valid-base64!!!\r\n"
    b"--BOUND--\r\n"
)


def _build_pipeline(*, dlq_store=None):
    return Pipeline(
        provider=MemoryProvider(seed={S: [SeedEmail("poison1", POISON_RAW)]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=MemoryEmitter(),
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant=TENANT),
        dlq_store=dlq_store,
        observers=Observers(),
    )


def test_one_poison_one_dlq_count():
    pipeline = _build_pipeline()
    report = pipeline.run_once()
    assert report.dead_lettered == 1
    assert len(report.dlq) == 1
    assert report.emitted == 0


def test_dlq_record_carries_raw():
    dlq_store = InMemoryDeadLetterStore()
    pipeline = _build_pipeline(dlq_store=dlq_store)
    pipeline.run_once()
    records = dlq_store.list_pending()
    assert len(records) == 1
    assert records[0].raw_b64  # non-empty: original RFC822 bytes preserved
    import base64
    assert base64.b64decode(records[0].raw_b64) == POISON_RAW


def test_re_dead_letter_idempotent():
    dlq_store = InMemoryDeadLetterStore()
    pipeline = _build_pipeline(dlq_store=dlq_store)
    msg = RawMessage(
        provider="memory", provider_message_id="poison1", stream=S,
        size_bytes=len(POISON_RAW), received_at=datetime.now(timezone.utc),
        cursor=Cursor(value="1", order=1), raw_bytes=POISON_RAW,
    )
    key = "acme|ops@acme.com|poison1"
    # Dead-letter the SAME message twice (white-box: exercises the documented
    # "put is idempotent by record_id" contract in Pipeline._dead_letter).
    pipeline._dead_letter(  # noqa: SLF001 - intentional white-box test of the redrive seam
        "poison1", msg, key, RunReport(), reason="first",
    )
    pipeline._dead_letter(  # noqa: SLF001
        "poison1", msg, key, RunReport(), reason="second",
    )
    records = dlq_store.list_pending()
    assert len(records) == 1  # overwritten, not duplicated
    assert records[0].reason == "second"
