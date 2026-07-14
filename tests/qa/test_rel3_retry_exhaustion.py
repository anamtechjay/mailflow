"""REL-3 / finding I5+P1 -- retry-exhaustion-to-DLQ across SEPARATE runs.

`InMemoryDedupeStore.release()` used to `del` the whole claim record, wiping its
`attempts` counter. Since `try_claim` only ever succeeds when the key is ABSENT,
each new run recreated the key fresh at `attempts=0`, so `attempts` could never
reach `PipelineConfig.max_attempts` (default 3) across separate `run_once()`
calls -- exactly the real redelivery path (a cron/worker re-invoking the
pipeline). A message whose extraction always raises a transient error would
retry FOREVER and never reach the DLQ: silent, unbounded retry, not silent
data loss in the sense of dropping the message, but an availability/poison-
message hazard the spec's bounded-retry contract (§A2) exists to prevent.

This reproducer drives the pipeline over SHARED dedupe+cursor stores across
multiple independent `Pipeline`/`run_once()` calls (mirroring
`test_f13_errors.py::test_transient_then_success`'s pattern of rebuilding the
pipeline per "run" against shared stores) with the DEFAULT `max_attempts=3`,
and asserts the poison message is dead-lettered by the 3rd attempt and stops
being retried.
"""

from __future__ import annotations

import pytest

from mailflow.core.errors import TransientError
from mailflow.core.models import CleanEmail, Envelope, RawMessage, StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

TENANT = "acme"
S = StreamRef(mailbox="ops@acme.com", folder="inbox")


class _AlwaysTransientExtractor:
    """`ContentExtractor` that raises `TransientError` on every call, forever --
    the "poison message that never recovers" case. Unlike
    `tests._harness.fakes.FaultExtractor` (fails its first N calls, then
    succeeds), this never succeeds, so it can only reach a terminal state via
    retry exhaustion -> DLQ, never via a later success."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, msg: RawMessage, env: Envelope) -> CleanEmail:
        self.calls += 1
        raise TransientError(f"persistent synthetic failure (call {self.calls})")


@pytest.mark.reliability
def test_poison_reaches_dlq_at_max_attempts(sink):
    fx = _AlwaysTransientExtractor()
    seed = {S: [SeedEmail("poison1", raw("poison1"))]}
    stores = dict(cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore())

    reports = []
    for _ in range(5):
        # Fresh Pipeline object each iteration, sharing only the dedupe/cursor
        # stores -- this is the real redelivery path: a separate run/process
        # invocation, NOT the same in-process retry loop.
        report = build_memory_pipeline(
            seed=seed, emitter=sink, extractor=fx, stores=stores,
        ).run_once()
        reports.append(report)
        if report.dead_lettered:
            break

    # It must have dead-lettered, and within max_attempts (3) runs -- not retried
    # forever.
    dlq_runs = [r for r in reports if r.dead_lettered]
    assert len(dlq_runs) == 1, (
        f"expected exactly one run to dead-letter the poison message within "
        f"max_attempts runs, got {len(dlq_runs)} across {len(reports)} runs"
    )
    assert dlq_runs[0].dead_lettered == 1
    exhausting_run_index = reports.index(dlq_runs[0])
    assert exhausting_run_index < 3, (
        f"expected exhaustion by the 3rd run (default max_attempts=3), "
        f"got it on run index {exhausting_run_index}"
    )

    # Retries must have stopped: the cursor advanced past the poison message,
    # and a further run_once() does not touch it again (no further attempts).
    cursor = stores["cursor_store"].get(TENANT, S)
    assert cursor is not None and cursor.order == 1

    calls_before_extra_run = fx.calls
    extra_report = build_memory_pipeline(
        seed=seed, emitter=sink, extractor=fx, stores=stores,
    ).run_once()
    assert extra_report.fetched == 0  # nothing left to fetch past the cursor
    assert fx.calls == calls_before_extra_run  # no further extraction attempts
