"""INT-7 — a long unattended run (soak). Drives many messages with monotonically
increasing receipt times (deterministic FakeClock, not wall time) and asserts every one
is delivered exactly once. Documents that the in-memory dedupe store retains a `done`
record per message (no TTL expiry yet) — the known unbounded-growth characteristic an
operator must plan for on a weeks-long run (use a store with TTL/eviction in production).
"""

from __future__ import annotations

from mailflow.core.models import StreamRef
from mailflow.stores.memory import InMemoryDedupeStore

from tests._harness.fakes import build_memory_pipeline
from tests._harness.reliability import FakeClock, soak_seed

S = StreamRef(mailbox="soak@acme.com", folder="inbox")


def test_soak_delivers_every_message_once(sink):
    n = 750
    seed = soak_seed(n, FakeClock(start=1_000_000.0), stream=S, step_seconds=2.0)
    dedupe = InMemoryDedupeStore()

    report = build_memory_pipeline(
        seed=seed, emitter=sink, stores=dict(dedupe_store=dedupe)
    ).run_once()

    assert report.emitted == n
    assert report.duplicates == 0
    assert len(sink.events) == n

    # Characterization of the INT-7 growth gap: one retained claim per message. A store
    # with TTL/eviction is required for an indefinite run; this pins the current shape.
    assert len(dedupe._claims) == n  # noqa: SLF001 - deliberately asserting the growth
