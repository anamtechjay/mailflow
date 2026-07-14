"""OBS-4 — health() must be able to report INGESTION liveness, not just store
reachability. Today it returns green even when the consume loop is stalled (e.g. Graph
subscriptions silently lapsed): all stores are reachable, so healthy=True — a false
all-clear. Given a last-activity timestamp + an idle budget, health() adds an
`ingestion` check that goes unhealthy when the loop has been silent too long.

Backward-compatible: with no liveness args, the ingestion check is absent and behavior
is unchanged.
"""

from __future__ import annotations

from mailflow.core.observability import health
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore


def _stores():
    return dict(
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
    )


def test_fresh_activity_is_healthy():
    report = health(**_stores(), last_activity_at=1000.0, now=1010.0, max_idle_seconds=300)
    assert report.checks["ingestion"] == "ok"
    assert report.healthy is True


def test_stale_activity_is_unhealthy_even_with_reachable_stores():
    # 900s since last activity, budget 300 → stalled ingestion, yet stores are fine.
    report = health(**_stores(), last_activity_at=1000.0, now=1900.0, max_idle_seconds=300)
    assert report.checks["ingestion"].startswith("stale")
    assert report.healthy is False


def test_never_active_is_unhealthy():
    report = health(**_stores(), last_activity_at=None, now=1900.0, max_idle_seconds=300)
    assert report.checks["ingestion"].startswith("stale")
    assert report.healthy is False


def test_no_liveness_args_is_backward_compatible():
    report = health(**_stores())
    assert "ingestion" not in report.checks
    assert report.healthy is True
