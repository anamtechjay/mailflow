"""Task 2c: health() probes core store dependencies via their ports and reports
reachability without corrupting state."""

from __future__ import annotations

from mailflow.core.observability import HealthReport, health
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)


def test_health_all_reachable() -> None:
    report = health(
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
    )
    assert isinstance(report, HealthReport)
    assert report.healthy is True
    assert report.checks["cursor"] == "ok"
    assert report.checks["dedupe"] == "ok"
    assert report.checks["blob"] == "ok"


class _BrokenCursor:
    def get(self, tenant, stream):  # type: ignore[no-untyped-def]
        raise RuntimeError("db unreachable")

    def commit_if_ahead(self, tenant, stream, cursor):  # type: ignore[no-untyped-def]
        return False


def test_health_reports_unreachable_dependency() -> None:
    report = health(
        cursor_store=_BrokenCursor(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
    )
    assert report.healthy is False
    assert "db unreachable" in report.checks["cursor"]


def test_health_does_not_corrupt_dedupe_state() -> None:
    # the probe must claim+release so a real key can still be claimed afterwards.
    dedupe = InMemoryDedupeStore()
    health(cursor_store=InMemoryCursorStore(), dedupe_store=dedupe,
           blob_store=InMemoryBlobStore())
    assert dedupe.try_claim("__healthcheck__", 1) is True  # probe released it


class _FlakyReleaseDedupe(InMemoryDedupeStore):
    def release(self, key: str) -> None:  # type: ignore[override]
        raise RuntimeError("release failed")


def test_health_dedupe_release_failure_does_not_crash_probe() -> None:
    # try_claim works; release raises. The probe must not crash and must still report ok
    # for the claim liveness (release is best-effort cleanup).
    report = health(cursor_store=InMemoryCursorStore(),
                    dedupe_store=_FlakyReleaseDedupe(), blob_store=InMemoryBlobStore())
    assert report.checks["dedupe"] == "ok"  # claim succeeded; release failure is swallowed
