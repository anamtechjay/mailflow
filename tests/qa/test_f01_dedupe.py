"""F01 — Dedupe / claim (seam + property). See docs/qa-partA-coverage.md."""

from __future__ import annotations

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

S = StreamRef(mailbox="ops@acme.com", folder="inbox")
S2 = StreamRef(mailbox="ops2@acme.com", folder="inbox")


def test_same_message_twice_emits_once(sink):
    ded = InMemoryDedupeStore()
    seed = {S: [SeedEmail("m1", raw("m1"))]}
    build_memory_pipeline(
        seed=seed, emitter=sink,
        stores=dict(dedupe_store=ded, cursor_store=InMemoryCursorStore()),
    ).run_once()
    r2 = build_memory_pipeline(
        seed=seed, emitter=sink,
        stores=dict(dedupe_store=ded, cursor_store=InMemoryCursorStore()),
    ).run_once()
    assert len(sink.events) == 1
    assert r2.duplicates == 1


def test_same_msg_two_mailboxes_emits_twice(sink):
    # Same provider_message_id on two different mailboxes: idempotency_key includes
    # mailbox, so this counts as two distinct arrivals, not a duplicate (spec §6.1).
    seed = {
        S: [SeedEmail("m1", raw("m1"))],
        S2: [SeedEmail("m1", raw("m1"))],
    }
    report = build_memory_pipeline(seed=seed, emitter=sink).run_once()
    assert report.emitted == 2
    assert len(sink.events) == 2


def test_claim_is_exclusive_sequential():
    ded = InMemoryDedupeStore()
    assert ded.try_claim("k1", 300) is True
    assert ded.try_claim("k1", 300) is False  # second claim on same key rejected


def test_mark_done_suppresses_redelivery():
    ded = InMemoryDedupeStore()
    assert ded.try_claim("k1", 300) is True
    ded.mark_done("k1", 3600)
    # A "redelivery" of the same message tries to claim the same key again.
    assert ded.try_claim("k1", 300) is False


def test_release_keeps_lifetime_attempt_count():
    # CONTRACT (REL-3 / finding I5+P1, superseding the old "release resets the
    # counter" behavior): `attempts` is a LIFETIME counter for the key. release()
    # frees the CLAIM (so the key is claimable again -- a later redelivery can
    # retry) but does NOT reset `attempts`, since PipelineConfig.max_attempts must
    # be reachable across separate runs/redeliveries, not just within one claim.
    ded = InMemoryDedupeStore()
    ded.try_claim("k1", 300)
    assert ded.record_attempt("k1") == 1
    assert ded.record_attempt("k1") == 2
    ded.release("k1")
    # After release, the key is claimable again (the claim itself was freed)...
    assert ded.try_claim("k1", 300) is True
    # ...but the attempt count carries over instead of resetting to 0.
    assert ded.record_attempt("k1") == 3
