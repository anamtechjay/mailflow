"""Decision trace + run report (spec §8.5, §12). Every message produces a trace
regardless of outcome — this answers the #1 support question, "why was my email
dropped?"."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from pydantic import BaseModel, Field

from mailflow.core.events import SCHEMA_VERSION
from mailflow.core.models import Disposition, StreamRef

if TYPE_CHECKING:
    # Type-only imports: ports.py imports DeadLetterRecord from this module, so a
    # real top-level import here would create a cycle. health() does a deferred
    # runtime import of BlobStore for its isinstance check.
    from mailflow.core.ports import BlobStore, CursorStore, DedupeStore


class DecisionTrace(BaseModel):
    canonical_id: str
    tenant: str
    stream: str
    disposition: Disposition
    stage: str
    matched_filter: str = ""
    reason: str = ""
    relevance_score: float | None = None
    is_inline: bool = False


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
    attachments_stripped: int = 0
    traces: list[DecisionTrace] = Field(default_factory=list)
    stripped: list[DecisionTrace] = Field(default_factory=list)
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

    def record_stripped(self, trace: DecisionTrace) -> None:
        """Record one stripped-attachment trace.

        Appends the trace to `self.stripped` (NOT `self.traces`) so that
        `traces` contains only message-disposition events. Increments
        `attachments_stripped` only — the four message-disposition counters
        (emitted/dropped/duplicates/dead_lettered) are intentionally untouched:
        a message that had parts stripped is still emitted exactly once."""
        self.stripped.append(trace)
        self.attachments_stripped += 1

    def add_dead_letter(self, dead_letter: DeadLetter) -> None:
        self.dlq.append(dead_letter)
        self.dead_lettered += 1

    def all_terminal(self) -> bool:
        """True iff every fetched message reached a terminal disposition
        (emitted/dropped/duplicate/dead_lettered) in this run — i.e. none was left
        pending for a transient retry. A notification-transport runtime uses this to
        decide whether it is safe to ack/checkpoint the source event (REL-8): if a
        message is still pending, holding the checkpoint lets the event redeliver,
        and dedupe makes re-running any already-terminal siblings harmless."""
        terminal = self.emitted + self.dropped + self.duplicates + self.dead_lettered
        return self.fetched <= terminal

    def counters(self) -> dict[str, int]:
        """Dependency-free metrics seam: disposition counters keyed by the canonical
        Disposition names, for a metrics exporter to scrape after run_once()."""
        return {
            "fetched": self.fetched,
            "emitted": self.emitted,
            "dropped": self.dropped,
            "duplicate": self.duplicates,
            "dead_lettered": self.dead_lettered,
            "attachments_stripped": self.attachments_stripped,
        }


ReportObserver = Callable[["RunReport"], None]
TraceObserver = Callable[["DecisionTrace"], None]


@dataclass(frozen=True)
class Observers:
    """Optional consumer callbacks. on_trace fires once per message decision
    (emitted/dropped/duplicate/dead_lettered); on_report fires once per run_once()."""

    on_report: ReportObserver | None = None
    on_trace: TraceObserver | None = None

    def any(self) -> bool:
        return self.on_report is not None or self.on_trace is not None


def notify_observers(
    callback: "Callable[[object], None]", arg: object, *, logger: "logging.Logger"
) -> None:
    """Invoke a consumer callback defensively — a buggy callback must never break
    ingestion. Logs and swallows any exception; never re-raises."""
    try:
        callback(arg)
    except Exception:  # noqa: BLE001 - consumer code; isolate it from the pipeline
        logger.warning("observer callback %r raised", callback, exc_info=True)


_PROBE_TENANT = "__healthcheck__"
_PROBE_KEY = "__healthcheck__"
_PROBE_STREAM = StreamRef(mailbox="__healthcheck__", folder=None)


class HealthReport(BaseModel):
    healthy: bool
    checks: dict[str, str] = Field(default_factory=dict)


def health(
    *,
    cursor_store: CursorStore,
    dedupe_store: DedupeStore,
    blob_store: BlobStore,
    last_activity_at: float | None = None,
    now: float | None = None,
    max_idle_seconds: float | None = None,
) -> HealthReport:
    """Probe the core store dependencies via their ports. Reads are harmless; the
    dedupe probe claims+releases a reserved key so it never corrupts real state. A
    deep blob write-probe is deferred — blob is a structural (port) check here.

    OBS-4 ingestion liveness: when `now` and `max_idle_seconds` are supplied, an
    `ingestion` check is added — reachable stores are NOT enough, since the consume
    loop can stall (e.g. Graph subscriptions lapse) while every store stays reachable,
    a false all-clear. The check is `stale` when the loop has produced no activity
    (`last_activity_at`, wall clock) within the idle budget, or has never been active.
    With no liveness args the check is omitted (backward compatible).

    The ports import is function-local: ports.py imports DeadLetterRecord from this
    module, so a top-level `from mailflow.core.ports import ...` would be a cycle."""
    from mailflow.core.ports import BlobStore  # cycle-avoiding deferred import

    checks: dict[str, str] = {}

    try:
        cursor_store.get(_PROBE_TENANT, _PROBE_STREAM)  # read-only liveness
        checks["cursor"] = "ok"
    except Exception as exc:  # noqa: BLE001 - any failure means unreachable
        checks["cursor"] = f"error: {exc}"

    try:
        dedupe_store.try_claim(_PROBE_KEY, 1)
        checks["dedupe"] = "ok"
    except Exception as exc:  # noqa: BLE001 - any failure means unreachable
        checks["dedupe"] = f"error: {exc}"
    finally:
        try:
            dedupe_store.release(_PROBE_KEY)  # best-effort cleanup; recovers a stuck sentinel
        except Exception:  # noqa: BLE001 - release failure must not fail the probe
            pass

    checks["blob"] = "ok" if isinstance(blob_store, BlobStore) \
        else "error: does not satisfy BlobStore port"

    if now is not None and max_idle_seconds is not None:
        if last_activity_at is None:
            checks["ingestion"] = "stale: no activity recorded yet"
        else:
            idle = now - last_activity_at
            checks["ingestion"] = (
                "ok" if idle <= max_idle_seconds
                else f"stale: no activity for {idle:.0f}s (budget {max_idle_seconds:.0f}s)"
            )

    return HealthReport(healthy=all(v == "ok" for v in checks.values()), checks=checks)
