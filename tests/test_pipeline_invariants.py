"""End-to-end §8 correctness invariants, driven through the real Pipeline with
in-memory components: duplicate, oversized→DLQ, poison→DLQ, dropped, cursor advance.
"""

from __future__ import annotations

from typing import Any

from mailflow.core.models import Envelope, RawMessage
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import BlacklistFilter
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)
from mailflow.core.models import StreamRef

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def raw(mid: str, sender: str = "a@partner.com", subject: str = "hi") -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: {sender}\r\n"
            f"To: ops@acme.com\r\nSubject: {subject}\r\n\r\nbody").encode()


class RaisingExtractor:
    """A non-MimeExtractor that always fails (drives the retry/DLQ path)."""
    def extract(self, msg: RawMessage, env: Envelope) -> Any:
        raise ValueError("boom")


def build(*, seed, filters=None, extractor=None, max_message_bytes=50_000_000,
          max_attempts=3, cursor_store=None, dedupe_store=None):
    emit, dlq = MemoryEmitter(), MemoryEmitter()
    pipe = Pipeline(
        provider=MemoryProvider(seed=seed),
        parser=MimeEnvelopeParser(),
        filters=FilterChain(filters or []),
        extractor=extractor or MimeExtractor(),
        emitter=emit, dlq_emitter=dlq,
        cursor_store=cursor_store or InMemoryCursorStore(),
        dedupe_store=dedupe_store or InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme", max_message_bytes=max_message_bytes,
                              max_attempts=max_attempts),
    )
    return pipe, emit, dlq


def test_happy_path_emits_one() -> None:
    pipe, emit, dlq = build(seed={STREAM: [SeedEmail("m1", raw("m1"))]})
    r = pipe.run_once()
    assert r.fetched == 1 and r.emitted == 1 and len(emit.events) == 1 and not dlq.events


def test_duplicate_same_message_id_emitted_once() -> None:
    pipe, emit, dlq = build(seed={STREAM: [SeedEmail("m1", raw("m1")), SeedEmail("m1", raw("m1"))]})
    r = pipe.run_once()
    assert r.fetched == 2 and r.emitted == 1 and r.duplicates == 1 and len(emit.events) == 1


def test_oversized_dead_lettered_without_extract() -> None:
    pipe, emit, dlq = build(seed={STREAM: [SeedEmail("m1", raw("m1"))]}, max_message_bytes=10)
    r = pipe.run_once()
    assert r.emitted == 0 and r.dead_lettered == 1 and len(dlq.events) == 1


def test_dropped_by_blacklist_filter() -> None:
    pipe, emit, dlq = build(
        seed={STREAM: [SeedEmail("m1", raw("m1", "x@spam.com"))]},
        filters=[BlacklistFilter(domains={"spam.com"})],
    )
    r = pipe.run_once()
    assert r.dropped == 1 and r.emitted == 0 and not emit.events


def test_poison_dead_lettered_at_max_attempts() -> None:
    pipe, emit, dlq = build(
        seed={STREAM: [SeedEmail("m1", raw("m1"))]},
        extractor=RaisingExtractor(), max_attempts=1,
    )
    r = pipe.run_once()
    assert r.emitted == 0 and r.dead_lettered == 1 and len(dlq.events) == 1


def test_transient_failure_is_not_terminal_cursor_stays() -> None:
    cur = InMemoryCursorStore()
    pipe, emit, dlq = build(
        seed={STREAM: [SeedEmail("m1", raw("m1"))]},
        extractor=RaisingExtractor(), max_attempts=3, cursor_store=cur,
    )
    r = pipe.run_once()
    assert r.emitted == 0 and r.dead_lettered == 0          # not terminal yet
    assert cur.get("acme", STREAM) is None                  # cursor did NOT advance


def test_cursor_advances_on_emit() -> None:
    cur = InMemoryCursorStore()
    pipe, _, _ = build(seed={STREAM: [SeedEmail("m1", raw("m1"))]}, cursor_store=cur)
    pipe.run_once()
    got = cur.get("acme", STREAM)
    assert got is not None and got.order >= 1


def test_cursor_advances_even_when_dropped() -> None:
    cur = InMemoryCursorStore()
    pipe, _, _ = build(
        seed={STREAM: [SeedEmail("m1", raw("m1", "x@spam.com"))]},
        filters=[BlacklistFilter(domains={"spam.com"})], cursor_store=cur,
    )
    pipe.run_once()
    assert cur.get("acme", STREAM) is not None              # dropped still advances (§8.1)
