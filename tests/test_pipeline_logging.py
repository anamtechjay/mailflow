"""Task 2a: the pipeline emits a structured log line per disposition and NEVER
logs the email body (PII scrubbing is deferred, but bodies are never logged)."""

from __future__ import annotations

import logging

from mailflow.core.models import StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")
SECRET_BODY = "TOP-SECRET-BODY-DO-NOT-LOG"


def _raw() -> bytes:
    return (
        "Message-ID: <m1@example.com>\r\nFrom: alice@partner.com\r\n"
        f"To: ops@acme.com\r\nSubject: hi\r\n\r\n{SECRET_BODY}\r\n"
    ).encode()


def _pipeline() -> Pipeline:
    return Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", _raw())]}),
        parser=MimeEnvelopeParser(), filters=FilterChain([]),
        extractor=MimeExtractor(), emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="acme"),
    )


def test_emitted_disposition_is_logged(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.INFO, logger="mailflow.pipeline"):
        _pipeline().run_once()
    records = [r for r in caplog.records if r.name == "mailflow.pipeline"]
    assert any("emitted" in r.getMessage() for r in records)


def test_body_is_never_logged(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.DEBUG, logger="mailflow.pipeline"):
        _pipeline().run_once()
    assert SECRET_BODY not in caplog.text
