"""Decision trace + run report (spec §8.5, §12). Every message produces a trace
regardless of outcome — this answers the #1 support question, "why was my email
dropped?"."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from mailflow.core.events import SCHEMA_VERSION
from mailflow.core.models import Disposition


class DecisionTrace(BaseModel):
    canonical_id: str
    tenant: str
    stream: str
    disposition: Disposition
    stage: str
    matched_filter: str = ""
    reason: str = ""
    relevance_score: float | None = None


class DeadLetter(BaseModel):
    canonical_id: str
    reason: str
    provider_message_id: str


class DeadLetterRecord(BaseModel):
    """A durable, replayable dead-letter: everything needed to rebuild the original
    RawMessage and re-submit it through the pipeline once the root cause is fixed.

    `record_id` is the idempotency_key (tenant, mailbox, provider_message_id), so a
    re-dead-letter of the same message overwrites rather than duplicates. `raw_b64` is
    the base64 of the original RFC822 bytes (may be empty if the message had none)."""

    record_id: str
    tenant: str
    provider: str
    provider_message_id: str
    mailbox: str
    folder: str | None = None
    canonical_id: str
    reason: str
    error_class: str = ""
    attempts: int = 0
    size_bytes: int = 0
    thread_key: str = ""
    cursor_value: str = ""
    cursor_order: int = 0
    received_at: datetime
    dead_lettered_at: datetime
    raw_b64: str = ""
    schema_version: str = SCHEMA_VERSION


class RunReport(BaseModel):
    fetched: int = 0
    emitted: int = 0
    dropped: int = 0
    duplicates: int = 0
    dead_lettered: int = 0
    traces: list[DecisionTrace] = Field(default_factory=list)
    dlq: list[DeadLetter] = Field(default_factory=list)

    def record(self, trace: DecisionTrace) -> None:
        self.traces.append(trace)
        if trace.disposition is Disposition.emitted:
            self.emitted += 1
        elif trace.disposition is Disposition.dropped:
            self.dropped += 1
        elif trace.disposition is Disposition.duplicate:
            self.duplicates += 1
        # The dead_lettered count is owned solely by add_dead_letter (every
        # dead-letter goes through the DLQ), so record() does NOT count it here —
        # the pipeline calls both add_dead_letter() and record(dead_lettered) for
        # a single poison message, and counting in both would double it.

    def add_dead_letter(self, dead_letter: DeadLetter) -> None:
        self.dlq.append(dead_letter)
        self.dead_lettered += 1
