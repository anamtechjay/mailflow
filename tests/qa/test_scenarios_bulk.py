"""Scenario — bulk volume. See docs/qa-partA-coverage.md ("Scenarios & invariants").

10,000 distinct messages on one stream, single `run_once()` pass: every message
is emitted exactly once and `canonical_id`s are all distinct (no accidental
collisions from the bulk id scheme).
"""

from __future__ import annotations

import pytest

from mailflow.core.models import StreamRef

from tests._harness.corpus import bulk_seed
from tests._harness.fakes import build_memory_pipeline

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


@pytest.mark.slow
def test_bulk_10k_emitted_once(sink):
    seed = bulk_seed(10_000, S)
    report = build_memory_pipeline(seed=seed, emitter=sink).run_once()

    assert report.emitted == 10_000
    assert report.duplicates == 0
    assert report.dead_lettered == 0
    assert len(sink.events) == 10_000

    canonical_ids = {event.email.canonical_id for event in sink.events}
    assert len(canonical_ids) == 10_000
