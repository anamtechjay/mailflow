"""Task 5: pipeline observability for stripped attachments.

When a MimeExtractor with an AttachmentPolicy strips parts, the pipeline must:
  - increment RunReport.attachments_stripped by N (one per stripped part)
  - NOT increment emitted/dropped/duplicates/dead_lettered beyond their normal values
  - append a per-strip DecisionTrace carrying reason + is_inline
  - NOT emit any dead_lettered trace for a strip

A clean run (no strips) must report attachments_stripped == 0 and leave
all disposition counts unchanged.
"""

from __future__ import annotations

import base64

from mailflow.core.models import Disposition, StripReason, StreamRef
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)
from mailflow.core.pipeline import Pipeline, PipelineConfig

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")

# ~20-byte inline PNG — stripped when inline.max_bytes < 20
PNG = b"\x89PNG\r\n\x1a\n png pixel bytes"


def _b64(data: bytes) -> bytes:
    return base64.b64encode(data)


def _multipart_with_inline(inline_data: bytes = PNG) -> bytes:
    """Minimal RFC822 multipart/mixed: text body + one inline PNG."""
    return (
        b"Message-ID: <strip-test@x>\r\n"
        b"From: a@partner.com\r\n"
        b"To: ops@acme.com\r\n"
        b"Subject: strip me\r\n"
        b'Content-Type: multipart/mixed; boundary="BB"\r\n'
        b"\r\n"
        b"--BB\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"hello body\r\n"
        b"--BB\r\n"
        b'Content-Type: image/png; name="logo.png"\r\n'
        b"Content-ID: <logo1>\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _b64(inline_data) + b"\r\n"
        b"--BB--\r\n"
    )


def _raw_no_attachments() -> bytes:
    """Plain-text message with no attachments."""
    return (
        b"Message-ID: <plain@x>\r\n"
        b"From: a@partner.com\r\n"
        b"To: ops@acme.com\r\n"
        b"Subject: plain\r\n"
        b"\r\n"
        b"just text"
    )


def _build(
    raw: bytes,
    *,
    extractor: MimeExtractor | None = None,
) -> tuple[Pipeline, MemoryEmitter, MemoryEmitter]:
    emit, dlq = MemoryEmitter(), MemoryEmitter()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw)]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=extractor or MimeExtractor(),
        emitter=emit,
        dlq_emitter=dlq,
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )
    return pipe, emit, dlq


# ---------------------------------------------------------------------------
# Core: strip observability
# ---------------------------------------------------------------------------


def test_strip_increments_attachments_stripped_and_not_other_counters() -> None:
    """A pipeline run that strips 1 part: attachments_stripped==1, emitted==1,
    dropped/duplicates/dead_lettered all remain 0."""
    policy = AttachmentPolicy(inline=AttachmentRule(max_bytes=4))
    ext = MimeExtractor(attachment_policy=policy)
    pipe, emit, dlq = _build(_multipart_with_inline(), extractor=ext)

    report = pipe.run_once()

    assert report.attachments_stripped == 1, "strip counter must be exactly 1"
    assert report.emitted == 1, "message must still be emitted"
    assert report.dropped == 0
    assert report.duplicates == 0
    assert report.dead_lettered == 0
    # emitter received the event (email was delivered)
    assert len(emit.events) == 1
    assert not dlq.events


def test_clean_run_reports_zero_stripped() -> None:
    """A plain-text message with no attachments must report attachments_stripped == 0."""
    pipe, emit, _ = _build(_raw_no_attachments())
    report = pipe.run_once()

    assert report.attachments_stripped == 0
    assert report.emitted == 1


def test_no_dead_lettered_trace_emitted_for_a_strip() -> None:
    """No trace with disposition=dead_lettered must appear in report.traces for a strip."""
    policy = AttachmentPolicy(inline=AttachmentRule(max_bytes=4))
    ext = MimeExtractor(attachment_policy=policy)
    pipe, _, _ = _build(_multipart_with_inline(), extractor=ext)

    report = pipe.run_once()

    dead_lettered_traces = [
        t for t in report.traces if t.disposition is Disposition.dead_lettered
    ]
    assert dead_lettered_traces == [], (
        "a strip must NEVER produce a dead_lettered trace"
    )


def test_per_strip_trace_carries_reason_and_is_inline() -> None:
    """The per-strip DecisionTrace must carry the StripReason string and is_inline flag."""
    policy = AttachmentPolicy(inline=AttachmentRule(max_bytes=4))
    ext = MimeExtractor(attachment_policy=policy)
    pipe, _, _ = _build(_multipart_with_inline(), extractor=ext)

    report = pipe.run_once()

    # Find the strip trace (stage == "attachment_strip")
    strip_traces = [t for t in report.traces if t.stage == "attachment_strip"]
    assert len(strip_traces) == 1

    st = strip_traces[0]
    assert st.reason == StripReason.oversize.value, "reason must be the StripReason name"
    assert st.is_inline is True, "inline attachment must be flagged is_inline=True"


def test_counters_includes_attachments_stripped() -> None:
    """counters() dict must expose attachments_stripped key."""
    policy = AttachmentPolicy(inline=AttachmentRule(max_bytes=4))
    ext = MimeExtractor(attachment_policy=policy)
    pipe, _, _ = _build(_multipart_with_inline(), extractor=ext)

    report = pipe.run_once()
    c = report.counters()

    assert "attachments_stripped" in c
    assert c["attachments_stripped"] == 1


def test_emitted_count_unchanged_with_multiple_stripped_parts() -> None:
    """N stripped parts on one message: attachments_stripped==N, emitted==1."""
    # Build a message with TWO inline images; policy strips both.
    def _two_inline() -> bytes:
        img = _b64(PNG)
        return (
            b"Message-ID: <two-strip@x>\r\n"
            b"From: a@partner.com\r\n"
            b"To: ops@acme.com\r\n"
            b"Subject: two strips\r\n"
            b'Content-Type: multipart/mixed; boundary="BB"\r\n'
            b"\r\n"
            b"--BB\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"body\r\n"
            b"--BB\r\n"
            b'Content-Type: image/png; name="a.png"\r\n'
            b"Content-ID: <img1>\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b"\r\n" + img + b"\r\n"
            b"--BB\r\n"
            b'Content-Type: image/png; name="b.png"\r\n'
            b"Content-ID: <img2>\r\n"
            b"Content-Transfer-Encoding: base64\r\n"
            b"\r\n" + img + b"\r\n"
            b"--BB--\r\n"
        )

    policy = AttachmentPolicy(inline=AttachmentRule(max_bytes=4))
    ext = MimeExtractor(attachment_policy=policy)
    pipe, emit, dlq = _build(_two_inline(), extractor=ext)

    report = pipe.run_once()

    assert report.attachments_stripped == 2
    assert report.emitted == 1
    assert report.dropped == 0
    assert report.dead_lettered == 0
    assert len(emit.events) == 1
    assert not dlq.events

    strip_traces = [t for t in report.traces if t.stage == "attachment_strip"]
    assert len(strip_traces) == 2
