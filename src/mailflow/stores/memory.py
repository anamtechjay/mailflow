"""In-memory store adapters. They encode the §8 invariants so the pipeline that
coordinates them can be tested deterministically with no cloud backend."""

from __future__ import annotations

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
    done: bool = False
    attempts: int = 0


class InMemoryDedupeStore:
    """Atomic claim-before-work (spec §8.2) + per-claim attempt counter (spec §8.4).

    Single-process in-memory model: a claim is exclusive until released or done.
    `lease_seconds`/`ttl_seconds` are accepted for interface parity with the real
    (Firestore/Redis) adapters; expiry is not simulated here.
    """

    def __init__(self) -> None:
        self._claims: dict[str, _ClaimRecord] = {}

    def try_claim(self, key: str, lease_seconds: int) -> bool:
        if key in self._claims:
            return False
        self._claims[key] = _ClaimRecord()
        return True

    def record_attempt(self, key: str) -> int:
        rec = self._claims.setdefault(key, _ClaimRecord())
        rec.attempts += 1
        return rec.attempts

    def mark_done(self, key: str, ttl_seconds: int) -> None:
        self._claims.setdefault(key, _ClaimRecord()).done = True

    def release(self, key: str) -> None:
        rec = self._claims.get(key)
        if rec is not None and not rec.done:
            del self._claims[key]


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
        self._blobs[ref] = b"".join(chunks)
        return ref

    def open(self, ref: str) -> Iterator[bytes]:
        yield self._blobs[ref]
