"""REL-1 / CUST-4 — a mid-batch transient failure must not let a later message's cursor
commit leapfrog the earlier failed one.

Batch [A, B]: A fails transiently (non-terminal, must be retried), B succeeds. The provider's
per-message cursor commit is monotonic, so B (order 2) advancing the cursor moves it PAST A
(order 1). On the next fetch the cursor is at 2 -> A (index 1) is skipped forever -> silently
lost, with no emit and no DLQ.

Fix: low-water-mark cursor — advance only through the contiguous prefix of terminal messages;
once a message is non-terminal, hold the cursor there for the rest of the batch so A is
re-fetched next run.
"""

from __future__ import annotations

import pytest

from mailflow.core.errors import TransientError
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline
from tests._harness.reliability import batch_with_faults

pytestmark = pytest.mark.reliability

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


def _subjects(sink) -> set[str]:
    return {e.email.subject for e in sink.events}


def test_midbatch_transient_does_not_skip_earlier(sink) -> None:
    seed = {S: [
        SeedEmail("A", raw("A", subject="msg-A")),
        SeedEmail("B", raw("B", subject="msg-B")),
    ]}
    stores = dict(cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore())

    # Run 1: A raises transient mid-batch; B succeeds.
    batch_with_faults(seed, fail={"A": TransientError("blip")}, emitter=sink, stores=stores).run_once()
    assert "msg-B" in _subjects(sink)          # B delivered
    assert "msg-A" not in _subjects(sink)       # A not yet (it failed)

    # Run 2: no fault, SAME stores. If A was not skipped, it is re-fetched and now delivered.
    build_memory_pipeline(seed=seed, emitter=sink, stores=stores).run_once()

    # The invariant: A must eventually be delivered — it must not have been silently skipped
    # when B's cursor leapfrogged it in run 1.
    assert "msg-A" in _subjects(sink), "A was silently skipped — B's cursor commit leapfrogged it"
