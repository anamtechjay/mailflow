"""DEP-2 — multiple replicas (separate processes) sharing one mailbox must not
double-emit. The correct topology is a SHARED, cross-process-atomic claim store; the
default in-memory store is per-process and would double-emit. `SqliteDedupeStore` backed
by one db file gives that atomicity (SQLite file locking), so two independent store
handles — modeling two replicas — can never both win the same claim.

`test_two_replicas_claim_each_message_once` proves the primitive; the pipeline-level test
proves the consequence: two pipelines that both fetch the same mail but share the dedupe
store emit each message exactly once across the fleet.
"""

from __future__ import annotations

import pytest

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore
from mailflow.stores.sqlite import SqliteDedupeStore

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

pytestmark = pytest.mark.reliability

S = StreamRef(mailbox="shared@acme.com", folder="inbox")


def test_two_replicas_claim_each_message_once(tmp_path):
    db = str(tmp_path / "dedupe.db")
    replica_a = SqliteDedupeStore(db_path=db)   # two independent handles = two processes
    replica_b = SqliteDedupeStore(db_path=db)

    a = replica_a.try_claim("msg-1", 300)
    b = replica_b.try_claim("msg-1", 300)

    assert [a, b].count(True) == 1              # exactly one replica wins — no double-processing


def test_two_pipelines_shared_dedupe_no_double_emit(tmp_path):
    db = str(tmp_path / "dedupe.db")
    seed = {S: [SeedEmail(f"m{i}", raw(f"m{i}")) for i in range(3)]}

    from mailflow.emit.memory import MemoryEmitter
    sink_a, sink_b = MemoryEmitter(), MemoryEmitter()

    # Two "replicas": each fetches all 3 messages (its own cursor) but they share ONE
    # cross-process dedupe store (two handles on the same file).
    stores_a = dict(cursor_store=InMemoryCursorStore(),
                    dedupe_store=SqliteDedupeStore(db_path=db), blob_store=InMemoryBlobStore())
    stores_b = dict(cursor_store=InMemoryCursorStore(),
                    dedupe_store=SqliteDedupeStore(db_path=db), blob_store=InMemoryBlobStore())

    report_a = build_memory_pipeline(seed=seed, emitter=sink_a, stores=stores_a).run_once()
    report_b = build_memory_pipeline(seed=seed, emitter=sink_b, stores=stores_b).run_once()

    # Across the fleet each of the 3 messages is emitted exactly once; the second
    # replica sees them all as duplicates.
    assert report_a.emitted + report_b.emitted == 3
    assert report_b.duplicates == 3
    all_ids = [e.email.provider_message_id for e in sink_a.events + sink_b.events]
    assert sorted(all_ids) == ["m0", "m1", "m2"]     # no id emitted twice


def test_per_process_memory_store_double_emits_across_replicas():
    """HAZARD PIN (DEP-2): the DEFAULT in-memory dedupe store is per-process, so two
    replicas each with their own store double-emit every message. This documents why
    memory-state is unsupported for a multi-replica deployment — a shared cross-process
    store (SqliteDedupeStore on a shared volume, or Firestore/Redis) is required."""
    from mailflow.emit.memory import MemoryEmitter
    from mailflow.stores.memory import InMemoryDedupeStore

    seed = {S: [SeedEmail("m0", raw("m0"))]}
    sink_a, sink_b = MemoryEmitter(), MemoryEmitter()
    # Separate in-memory dedupe stores == two isolated processes with no shared claim.
    build_memory_pipeline(seed=seed, emitter=sink_a,
                          stores=dict(dedupe_store=InMemoryDedupeStore())).run_once()
    build_memory_pipeline(seed=seed, emitter=sink_b,
                          stores=dict(dedupe_store=InMemoryDedupeStore())).run_once()

    # Both emit it — the double-emit the shared sqlite store above prevents.
    assert len(sink_a.events) == 1 and len(sink_b.events) == 1
