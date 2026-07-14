from mailflow.core.models import Disposition, StreamRef
from mailflow.core.observability import Observers
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore,
)

RAW = (b"From: a@partner.com\r\nTo: me@acme.com\r\nSubject: hi\r\n"
       b"Message-ID: <m1@partner.com>\r\n\r\nbody\r\n")


def _pipeline(observers: Observers, *, emitter=None) -> Pipeline:
    seed = {StreamRef(mailbox="me@acme.com", folder="inbox"): [SeedEmail("M1", RAW)]}
    return Pipeline(
        provider=MemoryProvider(seed=seed),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=emitter or MemoryEmitter(),
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
        observers=observers,
    )


def test_on_trace_fires_once_per_message():
    traces = []
    _pipeline(Observers(on_trace=traces.append)).run_once()
    assert len(traces) == 1
    assert traces[0].disposition is Disposition.emitted
    assert traces[0].canonical_id


def test_on_report_fires_once_per_run_with_counters():
    reports = []
    _pipeline(Observers(on_report=reports.append)).run_once()
    assert len(reports) == 1
    assert reports[0].emitted == 1


def test_throwing_on_trace_does_not_break_ingestion():
    def boom(_):
        raise RuntimeError("consumer bug")
    sink = MemoryEmitter()
    _pipeline(Observers(on_trace=boom), emitter=sink).run_once()  # must not raise
    assert len(sink.events) == 1  # email still emitted


def test_no_observers_is_a_noop():
    _pipeline(Observers()).run_once()  # no callbacks — must not raise
