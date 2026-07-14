"""In-memory store adapters. They encode the §8 invariants so the pipeline that
coordinates them can be tested deterministically with no cloud backend."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

from mailflow.core.models import Cursor, StreamRef
from mailflow.core.observability import DeadLetterRecord


class InMemoryCursorStore:
    """Per-(tenant, stream) cursor with monotonic compare-and-set (spec §8.3)."""

    def __init__(self) -> None:
        self._cursors: dict[tuple[str, str], Cursor] = {}

    def _key(self, tenant: str, stream: StreamRef) -> tuple[str, str]:
        return (tenant, stream.key)

    def get(self, tenant: str, stream: StreamRef) -> Cursor | None:
        return self._cursors.get(self._key(tenant, stream))

    def commit_if_ahead(self, tenant: str, stream: StreamRef, cursor: Cursor) -> bool:
        key = self._key(tenant, stream)
        current = self._cursors.get(key)
        if current is not None and cursor.order <= current.order:
            return False  # forward-only: reject stale/equal
        self._cursors[key] = cursor
        return True


@dataclass
class _ClaimRecord:
    claimed: bool = False
    done: bool = False
    attempts: int = 0
    expires_at: float | None = None


class InMemoryDedupeStore:
    """Atomic claim-before-work (spec §8.2) + per-claim attempt counter (spec §8.4).

    Single-process in-memory model: a claim is exclusive until released or done.
    `lease_seconds`/`ttl_seconds` are accepted for interface parity with the real
    (Firestore/Redis) adapters; expiry is not simulated here.

    CONTRACT (REL-3 / finding I5+P1): `attempts` is a LIFETIME counter for the key,
    not a per-in-process-claim counter. `release()` clears only the `claimed` flag
    and KEEPS the record (including `attempts`) -- it does not delete it. This is
    what lets `PipelineConfig.max_attempts` actually bound retries across separate
    `run_once()` calls (the real redelivery path: a worker crash, a cron
    re-invocation). The previous behavior (`del` on release) reset `attempts` to 0
    on every redelivery, so a persistently-failing message could retry forever and
    never reach the DLQ.
    """

    def __init__(self) -> None:
        self._claims: dict[str, _ClaimRecord] = {}

    def try_claim(self, key: str, lease_seconds: int) -> bool:
        rec = self._claims.get(key)
        if rec is None:
            self._claims[key] = _ClaimRecord(claimed=True)
            return True
        if rec.done or rec.claimed:
            return False
        rec.claimed = True
        return True

    def record_attempt(self, key: str) -> int:
        rec = self._claims.setdefault(key, _ClaimRecord(claimed=True))
        rec.attempts += 1
        return rec.attempts

    def mark_done(self, key: str, ttl_seconds: int) -> None:
        rec = self._claims.setdefault(key, _ClaimRecord())
        rec.done = True
        rec.expires_at = time.time() + ttl_seconds

    def release(self, key: str) -> None:
        rec = self._claims.get(key)
        if rec is not None and not rec.done:
            rec.claimed = False

    def purge_expired(self, *, now: float | None = None) -> int:
        """Delete done claims past their mark_done() ttl_seconds. Call periodically
        (e.g. a scheduled job) to bound this store's memory growth in a long-running
        process -- mark_done() alone does not expire anything on its own."""
        now = now if now is not None else time.time()
        expired = [
            key for key, rec in self._claims.items()
            if rec.done and rec.expires_at is not None and rec.expires_at <= now
        ]
        for key in expired:
            del self._claims[key]
        return len(expired)


class InMemoryDeadLetterStore:
    """Durable-shape DLQ store for tests + the zero-setup path. Keyed by record_id
    (insertion-ordered) so put overwrites and list_pending is deterministic."""

    def __init__(self) -> None:
        self._records: dict[str, DeadLetterRecord] = {}

    def put(self, record: DeadLetterRecord) -> None:
        self._records[record.record_id] = record

    def list_pending(self, *, limit: int | None = None) -> list[DeadLetterRecord]:
        records = list(self._records.values())
        return records if limit is None else records[:limit]

    def delete(self, record_id: str) -> None:
        self._records.pop(record_id, None)


@dataclass
class InMemoryBlobStore:
    """Stream attachment bytes to memory; return an opaque ref (spec §6.2)."""

    _blobs: dict[str, bytes] = field(default_factory=dict)

    def put_stream(self, ref: str, chunks: Iterator[bytes], content_type: str) -> str:
        if ref in self._blobs:
            return ref  # content-addressed dedupe: identical bytes already stored, skip
        self._blobs[ref] = b"".join(chunks)
        return ref

    def open(self, ref: str) -> Iterator[bytes]:
        yield self._blobs[ref]
