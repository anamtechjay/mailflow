"""Self-tests for the Phase-0 reliability harness (tests/_harness/reliability.py).

Each test proves the corresponding B1-B7 helper actually does what its docstring
claims -- these are load-bearing for Phase 1+ reproducers, so a false-positive here
(e.g. a fault that never fires, a claim that isn't actually stale) would silently
invalidate every later scenario built on top of it."""

from __future__ import annotations

import socket

import pytest

from mailflow.core.errors import TransientError
from mailflow.core.models import Cursor, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore
from mailflow.stores.sqlite import SqliteDedupeStore

from tests._harness.email_builder import raw
from tests._harness.reliability import (
    MIME_ADVERSARIAL,
    FakeClock,
    StubProvider,
    StubProviderRaising,
    batch_with_faults,
    cold_start,
    crash_between_claim_and_done,
    soak_seed,
    two_tenant_seed,
)

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


# =========================================================================== B1 batch_with_faults


def test_batch_with_faults_injects_mid_batch_fault_and_runs_real_pipeline():
    seed = {S: [SeedEmail("A", raw("A", subject="a")), SeedEmail("B", raw("B", subject="b"))]}
    pipeline = batch_with_faults(seed, fail={"A": TransientError("synthetic 429")})
    report = pipeline.run_once()

    # A failed (transient, released -> not terminal); B succeeded.
    assert report.fetched == 2
    assert report.emitted == 1
    assert isinstance(pipeline.extractor, object)
    assert pipeline.extractor.calls == ["A", "B"]  # type: ignore[attr-defined]


def test_batch_with_faults_injects_per_id_fault(sink):
    """B1 harness capability: batch_with_faults raises the chosen exception for the chosen
    message id within a real pipeline run, while other messages process normally. (REL-1 is
    now fixed — the dedicated reproducer is tests/qa/test_rel1_midbatch_cursor.py — so this
    self-test asserts the harness works, not that the defect persists.)"""
    seed = {S: [SeedEmail("A", raw("A", subject="a")), SeedEmail("B", raw("B", subject="b"))]}
    pipeline = batch_with_faults(seed, fail={"A": TransientError("synthetic 429")}, emitter=sink)
    pipeline.run_once()

    subjects = {e.email.subject for e in sink.events}
    assert subjects == {"b"}  # A faulted (not emitted); B processed normally

    # REL-1 low-water-mark fix: the cursor is held at/behind the faulted A, so A is not
    # skipped on restart — it remains re-fetchable.
    cursor = pipeline.cursor_store.get("acme", S)
    assert cursor is None or cursor.order < 1
    remaining_ids = [m.provider_message_id for m in pipeline.provider.fetch(S, cursor)]
    assert "A" in remaining_ids


def test_batch_with_faults_rejects_explicit_extractor_kwarg():
    with pytest.raises(ValueError):
        batch_with_faults({S: []}, fail={}, extractor=MimeExtractor())


# =========================================================================== B2 crash_between_claim_and_done


def test_crash_between_claim_and_done_leaves_stale_claim_on_same_store(tmp_path):
    store = SqliteDedupeStore(str(tmp_path / "dedupe.db"))
    crash_between_claim_and_done(store, "k1")

    # A second attempt against the SAME store handle sees the claim as still held --
    # mark_done was never called, so it can never be re-claimed (REL-2).
    assert store.try_claim("k1", 300) is False


def test_crash_between_claim_and_done_stale_claim_survives_new_store_handle(tmp_path):
    db_path = str(tmp_path / "dedupe.db")
    store = SqliteDedupeStore(db_path)
    crash_between_claim_and_done(store, "k2")

    # A brand-new store instance against the SAME db file (modeling an actual process
    # restart, not just object reuse) still sees the stale claim.
    restarted_store = SqliteDedupeStore(db_path)
    assert restarted_store.try_claim("k2", 300) is False


def test_crash_between_claim_and_done_raises_if_already_claimed(tmp_path):
    store = SqliteDedupeStore(str(tmp_path / "dedupe.db"))
    store.try_claim("k3", 300)
    with pytest.raises(AssertionError):
        crash_between_claim_and_done(store, "k3")


# =========================================================================== B3 FakeClock


def test_fake_clock_advance_moves_now():
    clock = FakeClock()
    t0 = clock.now()
    clock.advance(5.0)
    assert clock.now() == t0 + 5.0


def test_fake_clock_is_monotonic_compatible_callable():
    clock = FakeClock(start=100.0)
    t0 = clock()  # callable, zero-arg, like time.monotonic
    clock.advance(2.5)
    t1 = clock()
    assert t1 - t0 == 2.5


def test_fake_clock_default_starts_at_zero():
    assert FakeClock().now() == 0.0


# =========================================================================== B4 cold_start


def test_cold_start_gives_two_independent_fresh_handles():
    seed = {S: [SeedEmail(f"m{i}", raw(f"m{i}")) for i in range(3)]}
    first, second = cold_start(provider="memory", seed=seed)

    first_emails = first.fetch_new()
    second_emails = second.fetch_new()

    # Each cold start has fresh, empty state -- both emit ALL 3, none deduped against
    # the other. This is the loss/duplication a real cold start would risk (DEP-1/3).
    assert len(first_emails) == 3
    assert len(second_emails) == 3


def test_cold_start_ignores_caller_supplied_state():
    seed = {S: [SeedEmail("m0", raw("m0"))]}
    first, second = cold_start(provider="memory", seed=seed, state="sqlite:///should-be-ignored.db")
    # both still fresh/independent -- state= was forced to "memory", not honored as-is.
    assert len(first.fetch_new()) == 1
    assert len(second.fetch_new()) == 1


# =========================================================================== B5 MIME_ADVERSARIAL


def test_mime_adversarial_has_expected_cases():
    names = {entry["name"] for entry in MIME_ADVERSARIAL}
    assert names == {
        "unknown_charset",
        "inline_disposition_text",
        "forwarded_message_rfc822",
        "hostile_attachment_filename",
        "deeply_nested_multipart",
    }
    for entry in MIME_ADVERSARIAL:
        assert isinstance(entry["raw"], bytes) and entry["raw"]


def test_mime_adversarial_hostile_filename_is_actually_in_the_raw_bytes():
    entry = next(e for e in MIME_ADVERSARIAL if e["name"] == "hostile_attachment_filename")
    assert b"../../etc/passwd" in entry["raw"]


def test_mime_adversarial_forwarded_case_embeds_message_rfc822():
    entry = next(e for e in MIME_ADVERSARIAL if e["name"] == "forwarded_message_rfc822")
    assert b"Content-Type: message/rfc822" in entry["raw"]


@pytest.mark.parametrize("entry", MIME_ADVERSARIAL, ids=[e["name"] for e in MIME_ADVERSARIAL])
def test_mime_adversarial_entry_is_consumable_by_the_real_pipeline_without_crashing(entry):
    """Not asserting the (known-adversarial) content is handled *correctly* -- that's
    Phase 1+'s job. Only that the harness corpus is well-formed RFC822 the real
    MimeExtractor can be driven against inside a full Pipeline.run_once() without an
    unhandled exception escaping the process."""
    seed = {S: [SeedEmail(entry["name"], entry["raw"])]}
    from tests._harness.fakes import build_memory_pipeline

    report = build_memory_pipeline(seed=seed).run_once()
    assert report.fetched == 1


# =========================================================================== B6 StubProvider


def _pipeline_over(provider) -> Pipeline:
    return Pipeline(
        provider=provider,
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=MemoryEmitter(),
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )


def test_stub_provider_drives_a_full_pipeline_end_to_end():
    stream = StreamRef(mailbox="stub@acme.com", folder="inbox")
    seed = {stream: [SeedEmail(f"s{i}", raw(f"s{i}")) for i in range(2)]}
    provider = StubProvider(seed=seed)

    pipeline = _pipeline_over(provider)
    report = pipeline.run_once()

    assert provider.connected is True
    assert report.fetched == 2
    assert report.emitted == 2


def test_stub_provider_raising_propagates_native_error_out_of_run_once():
    provider = StubProviderRaising(error=socket.timeout("simulated timeout"))
    pipeline = _pipeline_over(provider)

    with pytest.raises(socket.timeout):
        pipeline.run_once()
    assert provider.connected is True


def test_stub_provider_raising_defaults_to_socket_timeout():
    provider = StubProviderRaising()
    assert isinstance(provider.error, socket.timeout)


# =========================================================================== B7 two_tenant_seed / soak_seed


def test_two_tenant_seed_shape():
    tenant_a, tenant_b, stream, seed = two_tenant_seed(3)
    assert tenant_a != tenant_b
    assert len(seed[stream]) == 3
    ids = [item.provider_message_id for item in seed[stream]]
    assert ids == ["SHARED0", "SHARED1", "SHARED2"]


def test_two_tenant_seed_pipelines_do_not_collide_across_tenants():
    """Two Pipelines differing ONLY in `PipelineConfig.tenant`, sharing the SAME
    dedupe_store/cursor_store instances, processing the SAME provider_message_ids --
    proves idempotency_key's tenant component keeps them from colliding (INT-2)."""
    tenant_a, tenant_b, stream, seed = two_tenant_seed(3)
    shared_dedupe = InMemoryDedupeStore()
    shared_cursor = InMemoryCursorStore()

    def _pipeline_for(tenant: str) -> Pipeline:
        return Pipeline(
            provider=StubProvider(seed=seed),
            parser=MimeEnvelopeParser(),
            filters=FilterChain([]),
            extractor=MimeExtractor(),
            emitter=MemoryEmitter(),
            dlq_emitter=MemoryEmitter(),
            cursor_store=shared_cursor,
            dedupe_store=shared_dedupe,
            blob_store=InMemoryBlobStore(),
            config=PipelineConfig(tenant=tenant),
        )

    report_a = _pipeline_for(tenant_a).run_once()
    report_b = _pipeline_for(tenant_b).run_once()

    # Neither tenant sees the other's claims/cursor as duplicates -- both emit all 3.
    assert report_a.emitted == 3
    assert report_a.duplicates == 0
    assert report_b.emitted == 3
    assert report_b.duplicates == 0


def test_soak_seed_returns_n_items_with_clock_driven_timestamps():
    clock = FakeClock(start=1_800_000_000.0)
    seed = soak_seed(5, clock, step_seconds=2.0)
    stream = StreamRef(mailbox="soak@acme.com", folder="inbox")

    items = seed[stream]
    assert len(items) == 5
    timestamps = [item.received_at for item in items]
    assert timestamps == sorted(timestamps)  # monotonically increasing
    assert (timestamps[1] - timestamps[0]).total_seconds() == 2.0
    # clock itself advanced by n * step_seconds
    assert clock.now() == 1_800_000_000.0 + 5 * 2.0


def test_soak_seed_custom_stream():
    clock = FakeClock()
    custom = StreamRef(mailbox="custom@acme.com", folder="in")
    seed = soak_seed(2, clock, stream=custom)
    assert set(seed) == {custom}
