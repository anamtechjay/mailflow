"""Edge-case coverage for the pipeline observer/report contract (spec §8.5, §12):
which disposition fires `on_trace` (dropped/tag/duplicate/dead_lettered/strip), the
`on_report` counters across mixed/empty batches, and the guardrail boundaries around
a misbehaving observer callback. Mirrors the setup pattern in
tests/core/test_pipeline_observers.py."""

from __future__ import annotations

import base64
import dataclasses
import logging

import pytest

from mailflow.core.models import Disposition, StreamRef
from mailflow.core.observability import Observers, notify_observers
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import BlacklistFilter
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _raw(mid: str = "m1", *, from_addr: str = "a@partner.com") -> bytes:
    return (
        f"Message-ID: <{mid}@x>\r\nFrom: {from_addr}\r\n"
        f"To: ops@acme.com\r\nSubject: hi\r\n\r\nbody"
    ).encode()


def _pipeline(
    *,
    observers: Observers,
    seed=None,
    filters=None,
    on_filtered: str = "tag",
    max_message_bytes: int = 50_000_000,
    emitter: MemoryEmitter | None = None,
    dedupe_store=None,
    cursor_store=None,
    extractor=None,
):
    """Build a real Pipeline over in-memory components (mirrors
    tests/core/test_pipeline_observers.py's `_pipeline` helper, extended with the
    knobs this file's edge cases need)."""
    emit = emitter or MemoryEmitter()
    dlq = MemoryEmitter()
    seed = seed if seed is not None else {STREAM: [SeedEmail("m1", _raw())]}
    pipe = Pipeline(
        provider=MemoryProvider(seed=seed),
        parser=MimeEnvelopeParser(),
        filters=FilterChain(filters or []),
        extractor=extractor or MimeExtractor(),
        emitter=emit,
        dlq_emitter=dlq,
        cursor_store=cursor_store or InMemoryCursorStore(),
        dedupe_store=dedupe_store or InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(
            tenant="acme", on_filtered=on_filtered, max_message_bytes=max_message_bytes,
        ),
        observers=observers,
    )
    return pipe, emit, dlq


# --------------------------------------------------------------------------- A. dispositions


def test_on_trace_delivers_dropped_disposition_on_filter_reject() -> None:
    traces = []
    pipe, emit, _dlq = _pipeline(
        observers=Observers(on_trace=traces.append),
        filters=[BlacklistFilter({"partner.com"})],
        on_filtered="drop",
    )
    report = pipe.run_once()
    assert [t.disposition for t in traces] == [Disposition.dropped]
    assert traces[0].matched_filter == "blacklist"
    assert traces[0].reason
    assert emit.events == []
    assert report.dropped == 1


def test_on_filtered_tag_delivers_email_and_fires_trace() -> None:
    traces = []
    pipe, emit, _dlq = _pipeline(
        observers=Observers(on_trace=traces.append),
        filters=[BlacklistFilter({"partner.com"})],
        on_filtered="tag",
    )
    pipe.run_once()
    assert len(traces) == 1
    assert len(emit.events) == 1
    email = emit.events[0].email
    assert email.disposition == "filtered"
    assert email.matched_filter == "blacklist"
    assert email.filter_reason


def test_on_trace_delivers_duplicate_on_redelivery() -> None:
    # Same dedupe_store shared across two pipeline instances/cursor stores simulates
    # a redelivery of the same message id: the second claim fails -> duplicate.
    dedupe = InMemoryDedupeStore()
    seed = {STREAM: [SeedEmail("m1", _raw())]}
    emit = MemoryEmitter()

    traces1 = []
    pipe1, _e1, _d1 = _pipeline(
        observers=Observers(on_trace=traces1.append), seed=seed,
        dedupe_store=dedupe, cursor_store=InMemoryCursorStore(), emitter=emit,
    )
    pipe1.run_once()
    assert [t.disposition for t in traces1] == [Disposition.emitted]

    traces2 = []
    pipe2, _e2, _d2 = _pipeline(
        observers=Observers(on_trace=traces2.append), seed=seed,
        dedupe_store=dedupe, cursor_store=InMemoryCursorStore(), emitter=emit,
    )
    pipe2.run_once()
    assert [t.disposition for t in traces2] == [Disposition.duplicate]
    assert len(emit.events) == 1  # still emitted only once overall


def test_on_trace_delivers_dead_lettered_for_oversized_message() -> None:
    traces = []
    pipe, emit, _dlq = _pipeline(
        observers=Observers(on_trace=traces.append),
        max_message_bytes=5,  # smaller than the seeded raw message
    )
    report = pipe.run_once()
    assert [t.disposition for t in traces] == [Disposition.dead_lettered]
    assert emit.events == []
    assert report.dead_lettered == 1


def test_on_report_counters_correct_for_mixed_batch() -> None:
    seed = {STREAM: [
        SeedEmail("keep1", _raw("keep1", from_addr="ok@good.com")),
        SeedEmail("drop1", _raw("drop1", from_addr="bad@partner.com")),
    ]}
    reports = []
    pipe, emit, _dlq = _pipeline(
        observers=Observers(on_report=reports.append), seed=seed,
        filters=[BlacklistFilter({"partner.com"})], on_filtered="drop",
    )
    pipe.run_once()
    assert len(reports) == 1
    assert reports[0].emitted == 1
    assert reports[0].dropped == 1
    assert len(emit.events) == 1


def test_on_report_fires_on_empty_run_with_zero_counters() -> None:
    reports = []
    pipe, _emit, _dlq = _pipeline(observers=Observers(on_report=reports.append), seed={})
    pipe.run_once()
    assert len(reports) == 1
    r = reports[0]
    assert (r.fetched, r.emitted, r.dropped, r.duplicates, r.dead_lettered,
            r.attachments_stripped) == (0, 0, 0, 0, 0, 0)


def test_on_trace_does_not_fire_for_attachment_strip() -> None:
    png = b"\x89PNG\r\n\x1a\n pixel-bytes"
    raw = (
        b"Message-ID: <strip@x>\r\nFrom: a@partner.com\r\nTo: ops@acme.com\r\n"
        b"Subject: strip\r\n"
        b'Content-Type: multipart/mixed; boundary="BB"\r\n\r\n'
        b"--BB\r\nContent-Type: text/plain\r\n\r\nhello body\r\n"
        b"--BB\r\n"
        b'Content-Type: image/png; name="logo.png"\r\n'
        b"Content-ID: <logo1>\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(png) + b"\r\n--BB--\r\n"
    )
    policy = AttachmentPolicy(inline=AttachmentRule(max_bytes=4))
    traces = []
    pipe, _emit, _dlq = _pipeline(
        observers=Observers(on_trace=traces.append),
        seed={STREAM: [SeedEmail("m1", raw)]},
        extractor=MimeExtractor(attachment_policy=policy),
    )
    report = pipe.run_once()
    # exactly ONE trace (the emitted disposition) — strips are recorded separately
    assert [t.disposition for t in traces] == [Disposition.emitted]
    assert report.attachments_stripped == 1


def test_on_trace_fires_in_order_once_per_message() -> None:
    seed = {STREAM: [SeedEmail(f"m{i}", _raw(f"m{i}")) for i in range(1, 4)]}
    traces = []
    pipe, emit, _dlq = _pipeline(observers=Observers(on_trace=traces.append), seed=seed)
    pipe.run_once()
    assert len(traces) == 3
    assert all(t.disposition is Disposition.emitted for t in traces)
    # trace order matches emission order (processed in fetch order)
    assert [t.canonical_id for t in traces] == [e.email.canonical_id for e in emit.events]


# --------------------------------------------------------------------------- B. guardrails


def test_throwing_on_report_does_not_break_run_once() -> None:
    def boom(_report):
        raise RuntimeError("bad on_report")

    pipe, emit, _dlq = _pipeline(observers=Observers(on_report=boom))
    report = pipe.run_once()  # must not raise
    assert report.emitted == 1
    assert len(emit.events) == 1


def test_throwing_on_trace_on_first_message_still_emits_second() -> None:
    calls = []

    def flaky(trace):
        calls.append(trace)
        if len(calls) == 1:
            raise RuntimeError("boom on first")

    seed = {STREAM: [SeedEmail("m1", _raw("m1")), SeedEmail("m2", _raw("m2"))]}
    pipe, emit, _dlq = _pipeline(observers=Observers(on_trace=flaky), seed=seed)
    report = pipe.run_once()  # must not raise
    assert report.emitted == 2
    assert len(emit.events) == 2
    assert len(calls) == 2


def test_notify_observers_does_not_swallow_base_exception() -> None:
    logger = logging.getLogger("mailflow.test.notify")

    def raises_keyboard(_arg):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        notify_observers(raises_keyboard, object(), logger=logger)


def test_notify_observers_swallows_normal_exception() -> None:
    logger = logging.getLogger("mailflow.test.notify")
    calls = []

    def raises_value_error(arg):
        calls.append(arg)
        raise ValueError("normal consumer bug")

    notify_observers(raises_value_error, "payload", logger=logger)  # must not raise
    assert calls == ["payload"]


def test_observers_is_frozen() -> None:
    obs = Observers()
    with pytest.raises(dataclasses.FrozenInstanceError):
        obs.on_report = lambda _r: None  # type: ignore[misc]
