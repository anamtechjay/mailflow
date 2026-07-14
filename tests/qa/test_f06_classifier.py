"""F06 — Classifier (unit). See docs/qa-partA-coverage.md.

`build_memory_pipeline` does not expose a `classifier` knob, so these tests build
a `Pipeline` directly (mirroring `build_memory_pipeline`'s wiring), as the
Phase-1 F12/F13 tests do for their own hand-built-Pipeline needs.
"""

from __future__ import annotations

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Envelope, Relevance, StreamRef, Verdict
from mailflow.core.observability import Observers
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import BlacklistFilter, WhitelistFilter
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.email_builder import raw

TENANT = "acme"
S = StreamRef(mailbox="ops@acme.com", folder="inbox")


class _SpyClassifier:
    def __init__(self, verdict: Verdict = Verdict.relevant, score: float = 0.9) -> None:
        self.calls = 0
        self._verdict = verdict
        self._score = score

    def classify(self, env: Envelope, ctx: FilterContext) -> Relevance:
        self.calls += 1
        return Relevance(verdict=self._verdict, score=self._score, reason="spy")


class _RaisingClassifier:
    def classify(self, env: Envelope, ctx: FilterContext) -> Relevance:
        raise RuntimeError("classifier blew up")


def _build_pipeline(*, filters, classifier, seed, emitter=None):
    return Pipeline(
        provider=MemoryProvider(seed=seed),
        parser=MimeEnvelopeParser(),
        filters=FilterChain(filters),
        extractor=MimeExtractor(),
        emitter=emitter or MemoryEmitter(),
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant=TENANT),
        classifier=classifier,
        observers=Observers(),
    )


def test_classifier_only_on_abstain():
    # Empty chain -> UNCERTAIN -> classifier runs.
    spy_uncertain = _SpyClassifier()
    seed_uncertain = {S: [SeedEmail("m1", raw("m1"))]}
    report_uncertain = _build_pipeline(
        filters=[], classifier=spy_uncertain, seed=seed_uncertain,
    ).run_once()
    assert spy_uncertain.calls == 1
    assert report_uncertain.emitted == 1

    # Decisive KEEP -> classifier never invoked.
    spy_keep = _SpyClassifier()
    seed_keep = {S: [SeedEmail("m2", raw("m2", sender="a@partner.com"))]}
    report_keep = _build_pipeline(
        filters=[WhitelistFilter({"partner.com"})], classifier=spy_keep, seed=seed_keep,
    ).run_once()
    assert spy_keep.calls == 0
    assert report_keep.emitted == 1

    # Decisive DROP -> classifier never invoked either.
    spy_drop = _SpyClassifier()
    seed_drop = {S: [SeedEmail("m3", raw("m3", sender="a@partner.com"))]}
    report_drop = _build_pipeline(
        filters=[BlacklistFilter({"partner.com"})], classifier=spy_drop, seed=seed_drop,
    ).run_once()
    assert spy_drop.calls == 0
    assert report_drop.dropped == 0  # default on_filtered="tag" -> delivered, not dropped
    assert report_drop.emitted == 1


def test_classifier_raise_contained():
    seed = {S: [SeedEmail("m1", raw("m1"))]}
    pipeline = _build_pipeline(filters=[], classifier=_RaisingClassifier(), seed=seed)
    report = pipeline.run_once()  # must not raise
    assert report.emitted == 0    # contained: caught as transient, released for retry
    assert report.dead_lettered == 0


def test_classifier_score_recorded():
    sink = MemoryEmitter()
    spy = _SpyClassifier(verdict=Verdict.relevant, score=0.42)
    seed = {S: [SeedEmail("m1", raw("m1"))]}
    _build_pipeline(filters=[], classifier=spy, seed=seed, emitter=sink).run_once()
    assert len(sink.events) == 1
    email = sink.events[0].email
    assert email.relevance.verdict == Verdict.relevant
    assert email.relevance.score == 0.42
