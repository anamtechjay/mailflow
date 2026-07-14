"""FIL-5 — a user-supplied filter that RAISES must not retry forever. It is treated as a
transient failure and, across redeliveries (shared dedupe store), reaches the DLQ within
`max_attempts` rather than looping unboundedly. This is the filter-side analogue of the
REL-3 extractor exhaustion guarantee.
"""

from __future__ import annotations

import pytest

from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import Envelope, StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

pytestmark = pytest.mark.reliability

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


class _RaisingFilter:
    name = "boom"

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        raise RuntimeError("user filter blew up")


def test_raising_filter_reaches_dlq_not_infinite_retry():
    stores = dict(cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore())
    seed = {S: [SeedEmail("m1", raw("m1"))]}

    reports = []
    for _ in range(6):
        r = build_memory_pipeline(
            seed=seed, filters=[_RaisingFilter()], stores=stores
        ).run_once()
        reports.append(r)
        if r.dead_lettered:
            break

    dlq_runs = [r for r in reports if r.dead_lettered]
    assert len(dlq_runs) == 1                      # exactly one run dead-letters it
    assert reports.index(dlq_runs[0]) < 3          # within default max_attempts=3, not forever
