"""B1: the size guard fails closed — an unknown/zero reported size is dead-lettered
BEFORE any download, while a normal-size message still emits."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Iterator

from mailflow.core.models import Cursor, RawMessage, StreamRef
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
RAW = (b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\n"
       b"To: ops@acme.com\r\nSubject: hi\r\n\r\nbody")


class UnknownSizeProvider:
    """Yields a real message but reports its size as 0/unknown (metadata gap)."""

    PROVIDER = "memory"

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def connect(self) -> None:
        return None

    def sync_streams(self) -> Iterable[StreamRef]:
        return [STREAM]

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        yield RawMessage(
            provider=self.PROVIDER, provider_message_id="m1", stream=stream,
            size_bytes=0,  # metadata could not determine the size
            received_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            cursor=Cursor(value=f"{stream.key}#1", order=1), raw_bytes=self._raw,
        )

    def message_size(self, msg: RawMessage) -> int | None:
        return 0  # unknown


def _pipe(provider: object) -> tuple[Pipeline, MemoryEmitter, MemoryEmitter]:
    emit, dlq = MemoryEmitter(), MemoryEmitter()
    pipe = Pipeline(
        provider=provider,  # type: ignore[arg-type]
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=emit, dlq_emitter=dlq,
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )
    return pipe, emit, dlq


def test_unknown_size_is_dead_lettered_fail_closed() -> None:
    pipe, emit, dlq = _pipe(UnknownSizeProvider(RAW))
    r = pipe.run_once()
    assert r.emitted == 0 and len(emit.events) == 0
    assert r.dead_lettered == 1 and len(dlq.events) == 1


def test_normal_size_still_emits() -> None:
    pipe, emit, dlq = _pipe(MemoryProvider(seed={STREAM: [SeedEmail("m1", RAW)]}))
    r = pipe.run_once()
    assert r.emitted == 1 and r.dead_lettered == 0 and len(emit.events) == 1
