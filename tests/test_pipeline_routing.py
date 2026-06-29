"""A2 typed-error routing through the real Pipeline (in-memory components):
PermanentError -> DLQ (no retry, counted once); AuthError/TransientError ->
retry while attempts remain, else DLQ; a generic Exception is treated as transient.
"""

from __future__ import annotations

from typing import Any

from mailflow.core.errors import AuthError, PermanentError, TransientError
from mailflow.core.models import Envelope, RawMessage, StreamRef
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


def raw(mid: str = "m1") -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: a@partner.com\r\n"
            f"To: ops@acme.com\r\nSubject: hi\r\n\r\nbody").encode()


class RaisingExtractor:
    """A non-MimeExtractor that fails with a chosen exception (drives A2 routing)."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def extract(self, msg: RawMessage, env: Envelope) -> Any:
        raise self.exc


def build(*, extractor: Any, max_attempts: int = 3,
          cursor_store: Any = None) -> tuple[Pipeline, MemoryEmitter, MemoryEmitter]:
    emit, dlq = MemoryEmitter(), MemoryEmitter()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=extractor,
        emitter=emit, dlq_emitter=dlq,
        cursor_store=cursor_store or InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme", max_attempts=max_attempts),
    )
    return pipe, emit, dlq


def test_permanent_error_dead_letters_immediately_counted_once() -> None:
    # max_attempts high: a PermanentError must NOT retry, and must count exactly once.
    pipe, emit, dlq = build(extractor=RaisingExtractor(PermanentError("nope")), max_attempts=5)
    r = pipe.run_once()
    assert r.emitted == 0
    assert r.dead_lettered == 1          # counted EXACTLY once
    assert len(dlq.events) == 1


def test_auth_error_retries_while_attempts_remain() -> None:
    cur = InMemoryCursorStore()
    pipe, emit, dlq = build(
        extractor=RaisingExtractor(AuthError("401")), max_attempts=3, cursor_store=cur,
    )
    r = pipe.run_once()
    assert r.emitted == 0 and r.dead_lettered == 0      # not terminal -> retry later
    assert cur.get("acme", STREAM) is None               # cursor did not advance


def test_auth_error_dead_letters_at_max_attempts() -> None:
    pipe, emit, dlq = build(extractor=RaisingExtractor(AuthError("401")), max_attempts=1)
    r = pipe.run_once()
    assert r.emitted == 0 and r.dead_lettered == 1 and len(dlq.events) == 1


def test_transient_error_retries_while_attempts_remain() -> None:
    cur = InMemoryCursorStore()
    pipe, emit, dlq = build(
        extractor=RaisingExtractor(TransientError("429")), max_attempts=3, cursor_store=cur,
    )
    r = pipe.run_once()
    assert r.emitted == 0 and r.dead_lettered == 0
    assert cur.get("acme", STREAM) is None


def test_transient_error_dead_letters_at_max_attempts() -> None:
    pipe, emit, dlq = build(extractor=RaisingExtractor(TransientError("429")), max_attempts=1)
    r = pipe.run_once()
    assert r.emitted == 0 and r.dead_lettered == 1 and len(dlq.events) == 1


def test_generic_exception_treated_as_transient() -> None:
    cur = InMemoryCursorStore()
    pipe, emit, dlq = build(
        extractor=RaisingExtractor(ValueError("boom")), max_attempts=3, cursor_store=cur,
    )
    r = pipe.run_once()
    assert r.dead_lettered == 0 and cur.get("acme", STREAM) is None
