"""F11 — Cursor (property + unit). See docs/qa-partA-coverage.md.

Cursor advances on ANY terminal disposition (emitted/dropped/duplicate/
dead_lettered), never on a non-terminal one, and only monotonically forward
(spec §8.1/§8.3).
"""

from __future__ import annotations

from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import Cursor, StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryCursorStore

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

TENANT = "acme"
S = StreamRef(mailbox="ops@acme.com", folder="inbox")


class _AlwaysDrop:
    name = "always_drop"

    def evaluate(self, env, ctx: FilterContext) -> FilterDecision:
        return FilterDecision.drop(self.name, "test drop")


def test_cursor_advances_on_emit(sink):
    cursor_store = InMemoryCursorStore()
    seed = {S: [SeedEmail("m1", raw("m1"))]}
    build_memory_pipeline(
        seed=seed, emitter=sink, stores=dict(cursor_store=cursor_store),
    ).run_once()
    assert cursor_store.get(TENANT, S) is not None
    assert cursor_store.get(TENANT, S).order == 1


def test_cursor_advances_on_drop(sink):
    cursor_store = InMemoryCursorStore()
    seed = {S: [SeedEmail("m1", raw("m1"))]}
    report = build_memory_pipeline(
        seed=seed, emitter=sink, stores=dict(cursor_store=cursor_store),
        filters=[_AlwaysDrop()], on_filtered="drop",
    ).run_once()
    assert report.dropped == 1
    assert cursor_store.get(TENANT, S) is not None
    assert cursor_store.get(TENANT, S).order == 1


def test_cursor_advances_on_dead_letter(sink):
    cursor_store = InMemoryCursorStore()
    seed = {S: [SeedEmail("m1", raw("m1", subject="hi", body="a bit of body"))]}
    report = build_memory_pipeline(
        seed=seed, emitter=sink, stores=dict(cursor_store=cursor_store),
        max_message_bytes=5,
    ).run_once()
    assert report.dead_lettered == 1
    assert cursor_store.get(TENANT, S) is not None
    assert cursor_store.get(TENANT, S).order == 1


def test_cursor_advances_on_duplicate(sink):
    cursor_store = InMemoryCursorStore()
    # Two seed entries sharing the same provider_message_id -> same idempotency_key.
    # The first claims + emits; the second is a duplicate, but is still terminal
    # and must still advance the cursor to its own (later) position.
    seed = {S: [SeedEmail("m1", raw("m1")), SeedEmail("m1", raw("m1"))]}
    report = build_memory_pipeline(
        seed=seed, emitter=sink, stores=dict(cursor_store=cursor_store),
    ).run_once()
    assert report.emitted == 1
    assert report.duplicates == 1
    assert cursor_store.get(TENANT, S) is not None
    assert cursor_store.get(TENANT, S).order == 2


def test_commit_if_ahead_rejects_regression():
    cursor_store = InMemoryCursorStore()
    assert cursor_store.commit_if_ahead(TENANT, S, Cursor(value="a", order=5)) is True
    # Lower order is rejected; store keeps the higher value.
    assert cursor_store.commit_if_ahead(TENANT, S, Cursor(value="b", order=3)) is False
    assert cursor_store.get(TENANT, S).order == 5
    # Equal order is also rejected (strictly-forward, not >=).
    assert cursor_store.commit_if_ahead(TENANT, S, Cursor(value="c", order=5)) is False
    assert cursor_store.get(TENANT, S).value == "a"
