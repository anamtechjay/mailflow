"""F05 — Filtering + on_filtered (unit + integration). See docs/qa-partA-coverage.md."""

from __future__ import annotations

from mailflow.core.models import StreamRef
from mailflow.filters.deterministic import BlacklistFilter, FunctionFilter, WhitelistFilter
from mailflow.providers.memory import SeedEmail

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


def test_drop_reports_dropped_and_suppresses(sink):
    traces = []
    seed = {S: [SeedEmail("m1", raw("m1", sender="a@partner.com"))]}
    r = build_memory_pipeline(
        seed=seed, emitter=sink, filters=[BlacklistFilter({"partner.com"})],
        on_filtered="drop", observers_on_trace=traces.append,
    ).run_once()
    assert sink.events == [] and r.dropped == 1
    assert traces[0].matched_filter == "blacklist" and traces[0].reason


def test_tag_delivers_marked_email(sink):
    # on_filtered="tag" (the default): the message is DELIVERED (not suppressed),
    # but stamped so the consumer can see it was matched by a filter.
    seed = {S: [SeedEmail("m1", raw("m1", sender="a@partner.com"))]}
    r = build_memory_pipeline(
        seed=seed, emitter=sink, filters=[BlacklistFilter({"partner.com"})], on_filtered="tag",
    ).run_once()
    assert r.emitted == 1
    assert len(sink.events) == 1
    email = sink.events[0].email
    assert email.disposition == "filtered"
    assert email.matched_filter == "blacklist"
    assert email.filter_reason


def test_empty_chain_keeps(sink):
    seed = {S: [SeedEmail("m1", raw("m1"))]}
    r = build_memory_pipeline(seed=seed, emitter=sink, filters=[]).run_once()
    assert r.emitted == 1
    assert sink.events[0].email.disposition == "emitted"


def test_chain_short_circuits(sink):
    # WhitelistFilter decides KEEP first (§7.4: first decisive verdict wins); a later
    # BlacklistFilter matching the SAME domain never runs because the chain stopped.
    seed = {S: [SeedEmail("m1", raw("m1", sender="a@partner.com"))]}
    r = build_memory_pipeline(
        seed=seed, emitter=sink,
        filters=[WhitelistFilter({"partner.com"}), BlacklistFilter({"partner.com"})],
    ).run_once()
    assert r.emitted == 1
    assert r.dropped == 0
    assert sink.events[0].email.matched_filter == "whitelist"


def test_function_filter_raise_contained(sink):
    # A filter raising is NOT a documented three-valued outcome; Pipeline._process's
    # catch-all `except Exception` treats it as transient (contained -> released for
    # retry, not delivered, not crashed, not dead-lettered on the first attempt).
    def _boom(env):
        raise RuntimeError("filter blew up")

    seed = {S: [SeedEmail("m1", raw("m1"))]}
    report = build_memory_pipeline(
        seed=seed, emitter=sink, filters=[FunctionFilter(_boom)],
    ).run_once()
    assert sink.events == []
    assert report.emitted == 0
    assert report.dead_lettered == 0  # not exhausted yet: released for a later retry


def test_blacklist_case_insensitive(sink):
    # The sender's domain is upper-case; BlacklistFilter/_domain() lowercase both
    # sides before comparing, so the match is case-insensitive either way.
    seed = {S: [SeedEmail("m1", raw("m1", sender="a@PARTNER.COM"))]}
    r = build_memory_pipeline(
        seed=seed, emitter=sink, filters=[BlacklistFilter({"partner.com"})], on_filtered="drop",
    ).run_once()
    assert r.dropped == 1
    assert sink.events == []
