"""Redrive — re-submit durable dead-letters through the pipeline once the root cause is
fixed (A2 recovery). Each record is rebuilt into its original RawMessage and run through a
FRESH, redrive-scoped Pipeline: a fresh DedupeStore (so the original marked-done claim
does not reject the replay) and a throwaway CursorStore, but the operator's REAL emitter /
dlq_emitter / blob_store. A record that reaches a non-DLQ terminal disposition is deleted
from the store; one that dead-letters again is kept for a later attempt.

At-least-once is preserved: the re-emitted event carries the original idempotency_key, so a
downstream consumer dedupes any message that was already partially delivered (A4)."""

from __future__ import annotations

import base64
from typing import Iterable, Iterator

from pydantic import BaseModel, Field

from mailflow.core.models import Cursor, RawMessage, StreamRef
from mailflow.core.observability import DeadLetterRecord, RunReport
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.ports import (
    BlobStore,
    Classifier,
    ContentCleaner,
    ContentExtractor,
    DeadLetterStore,
    Emitter,
    EnvelopeParser,
)
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore


class RedriveReport(BaseModel):
    examined: int = 0
    resubmitted: int = 0          # reached a non-dead-letter terminal disposition
    still_dead_lettered: int = 0
    run: RunReport = Field(default_factory=RunReport)


def rebuild_raw_message(record: DeadLetterRecord) -> RawMessage:
    return RawMessage(
        provider=record.provider,
        provider_message_id=record.provider_message_id,
        stream=StreamRef(mailbox=record.mailbox, folder=record.folder),
        size_bytes=record.size_bytes,
        received_at=record.received_at,
        cursor=Cursor(value=record.cursor_value, order=record.cursor_order),
        raw_bytes=base64.b64decode(record.raw_b64) if record.raw_b64 else b"",
        thread_key=record.thread_key,
    )


class _RedriveProvider:
    """A one-shot MailboxProvider that re-yields exactly the rebuilt RawMessages
    (preserving cursor + thread_key, which MemoryProvider/SeedEmail would lose)."""

    def __init__(self, messages: list[RawMessage]) -> None:
        self._by_stream: dict[StreamRef, list[RawMessage]] = {}
        for msg in messages:
            self._by_stream.setdefault(msg.stream, []).append(msg)

    def connect(self) -> None:
        return None

    def sync_streams(self) -> Iterable[StreamRef]:
        return list(self._by_stream)

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        for msg in self._by_stream.get(stream, []):
            yield msg

    def message_size(self, msg: RawMessage) -> int | None:
        return msg.size_bytes


def _accumulate(into: RunReport, part: RunReport) -> None:
    # NOTE: keep this field list in sync with RunReport. Merging two already-finalized
    # reports is safe — the frozen "only add_dead_letter increments" rule isn't violated
    # because both inputs are final counts, not live increments.
    into.fetched += part.fetched
    into.emitted += part.emitted
    into.dropped += part.dropped
    into.duplicates += part.duplicates
    into.dead_lettered += part.dead_lettered
    into.traces.extend(part.traces)
    into.dlq.extend(part.dlq)


def redrive(
    *,
    store: DeadLetterStore,
    emitter: Emitter,
    dlq_emitter: Emitter,
    blob_store: BlobStore,
    config: PipelineConfig,
    parser: EnvelopeParser | None = None,
    extractor: ContentExtractor | MimeExtractor | None = None,
    filters: FilterChain | None = None,
    cleaner: ContentCleaner | None = None,
    classifier: Classifier | None = None,
    limit: int | None = None,
) -> RedriveReport:
    parser = parser if parser is not None else MimeEnvelopeParser()
    extractor = extractor if extractor is not None else MimeExtractor()
    filters = filters if filters is not None else FilterChain([])

    report = RedriveReport()
    for record in store.list_pending(limit=limit):
        report.examined += 1
        msg = rebuild_raw_message(record)
        pipeline = Pipeline(
            provider=_RedriveProvider([msg]),
            parser=parser,
            filters=filters,
            extractor=extractor,
            emitter=emitter,
            dlq_emitter=dlq_emitter,
            cursor_store=InMemoryCursorStore(),   # throwaway: redrive does not own cursors
            dedupe_store=InMemoryDedupeStore(),    # fresh: bypass the original marked-done claim
            blob_store=blob_store,
            config=config,
            classifier=classifier,
            cleaner=cleaner,
        )
        run = pipeline.run_once()
        _accumulate(report.run, run)
        if run.fetched >= 1 and run.dead_lettered == 0:
            store.delete(record.record_id)
            report.resubmitted += 1
        else:
            report.still_dead_lettered += 1
    return report
