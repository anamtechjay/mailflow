"""Primary-store crash — a transient failure of the cursor or dedupe store must not
lose a message or emit it twice. Mirrors the existing DLQ-store-crash proof
(`tests/test_pipeline_dlq_durable.py`) for the cursor/dedupe stores.

These are characterization/regression tests: they pin the pipeline's already-safe
recovery contract (claim-before-spend + mark_done-before-cursor-commit + dedupe on
redelivery) so a future refactor can't silently break crash-safety. A run that raises
mid-stream leaves the message reclaimable; the next run reconciles via dedupe.
"""

from __future__ import annotations

import pytest

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

pytestmark = pytest.mark.reliability

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


class _FlakyCursorStore:
    """Wraps a real cursor store; raises on the first `commit_if_ahead`, then delegates.
    Models the cursor DB blipping right after a message was emitted + marked done."""

    def __init__(self) -> None:
        self._inner = InMemoryCursorStore()
        self.fail_next = True

    def get(self, tenant, stream):
        return self._inner.get(tenant, stream)

    def commit_if_ahead(self, tenant, stream, cursor):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("cursor store transient outage")
        return self._inner.commit_if_ahead(tenant, stream, cursor)


class _FlakyDedupeStore(InMemoryDedupeStore):
    """Raises on the first `try_claim`, then behaves normally. Models the dedupe DB
    blipping before any work is claimed/spent."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = True

    def try_claim(self, key: str, lease_seconds: int) -> bool:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("dedupe store transient outage")
        return super().try_claim(key, lease_seconds)


def test_cursor_commit_crash_then_recover_emits_exactly_once(sink):
    stores = dict(
        cursor_store=_FlakyCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=None,
    )
    stores = {k: v for k, v in stores.items() if v is not None}
    seed = {S: [SeedEmail("m1", raw("m1"))]}

    # Run 1: message is emitted + marked done, then the cursor commit blows up.
    with pytest.raises(RuntimeError):
        build_memory_pipeline(seed=seed, emitter=sink, stores=stores).run_once()
    assert len(sink.events) == 1                 # emitted once (before the crash)
    assert stores["cursor_store"].get("acme", S) is None  # cursor never advanced

    # Run 2 (same stores): message re-fetched (cursor unmoved) but dedupe catches it —
    # NOT re-emitted — and the cursor now advances cleanly.
    report = build_memory_pipeline(seed=seed, emitter=sink, stores=stores).run_once()
    assert report.duplicates == 1
    assert report.emitted == 0
    assert len(sink.events) == 1                 # still exactly one — no double-emit, no loss
    assert stores["cursor_store"].get("acme", S).order == 1


def test_dedupe_claim_crash_then_recover_delivers_once(sink):
    stores = dict(cursor_store=InMemoryCursorStore(), dedupe_store=_FlakyDedupeStore())
    seed = {S: [SeedEmail("m1", raw("m1"))]}

    # Run 1: try_claim raises before any spend → nothing emitted, nothing marked done.
    with pytest.raises(RuntimeError):
        build_memory_pipeline(seed=seed, emitter=sink, stores=stores).run_once()
    assert sink.events == []

    # Run 2: the message is still reclaimable → delivered exactly once. No loss.
    report = build_memory_pipeline(seed=seed, emitter=sink, stores=stores).run_once()
    assert report.emitted == 1
    assert len(sink.events) == 1
