"""Explicit 1,000-email bulk pass — a fast (non-slow) conservation check so the common
"process a thousand messages" case is guarded in every CI run (the 10k variant in
test_scenarios_bulk.py is marked slow and may be deselected).
"""

from __future__ import annotations

from mailflow.core.models import StreamRef

from tests._harness.corpus import bulk_seed
from tests._harness.fakes import build_memory_pipeline

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


def test_bulk_1k_emitted_once_conservation(sink):
    seed = bulk_seed(1_000, S)
    report = build_memory_pipeline(seed=seed, emitter=sink).run_once()

    assert report.emitted == 1_000
    assert report.duplicates == 0
    assert report.dead_lettered == 0
    # Conservation: everything fetched reached exactly one terminal disposition.
    assert report.fetched == report.emitted + report.dropped + report.duplicates + report.dead_lettered
    assert len(sink.events) == 1_000
    assert len({e.email.canonical_id for e in sink.events}) == 1_000
