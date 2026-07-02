"""Task 2c: health() probes core store dependencies via their ports and reports
reachability without corrupting state."""

from __future__ import annotations

import pytest

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


# --- convenience-wrapper delegations: Pipeline.health() and Mailflow.health() ---


def test_pipeline_health_delegates() -> None:
    # Pipeline.health() is a thin delegation to the module-level health(); build a
    # Pipeline with in-memory parts (mirrors tests/test_pipeline_logging.py::_pipeline).
    from mailflow.core.pipeline import Pipeline, PipelineConfig
    from mailflow.emit.memory import MemoryEmitter
    from mailflow.extract.envelope import MimeEnvelopeParser
    from mailflow.extract.mime import MimeExtractor
    from mailflow.filters.chain import FilterChain
    from mailflow.providers.memory import MemoryProvider

    pipe = Pipeline(
        provider=MemoryProvider(seed={}),
        parser=MimeEnvelopeParser(), filters=FilterChain([]),
        extractor=MimeExtractor(), emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="acme"),
    )
    report = pipe.health()
    assert isinstance(report, HealthReport) and report.healthy is True


def test_mailflow_health_via_connect() -> None:
    # connect("memory", ...) wires a store-backed handle; Mailflow.health() delegates.
    from mailflow import connect

    mf = connect("memory", seed={}, tenant="acme")
    report = mf.health()
    assert isinstance(report, HealthReport) and report.healthy is True


def test_mailflow_health_without_stores_raises() -> None:
    # Mailflow.health() raises RuntimeError when its dedupe/blob stores are absent.
    from mailflow import connect

    mf = connect("memory", seed={}, tenant="acme")
    mf._dedupe_store = None  # simulate a handle built without the stores
    with pytest.raises(RuntimeError):
        mf.health()
