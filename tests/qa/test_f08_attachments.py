"""F08 — Attachments & caps (unit + large-gen). See docs/qa-partA-coverage.md."""

from __future__ import annotations

from mailflow.core.models import Attachment, ScanVerdict, StreamRef
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule
from mailflow.extract.safety import check_allowlist
from mailflow.providers.memory import SeedEmail

from tests._harness.corpus import large_attachment_raw
from tests._harness.fakes import build_memory_pipeline

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


def test_over_attach_cap_stripped(sink):
    # Policy path: a per-attachment cap violation STRIPS the part; the message
    # itself is still delivered (extract/mime.py's `_process_policy`, never raises).
    seed = {S: [SeedEmail("big1", large_attachment_raw(1_000_000))]}
    policy = AttachmentRule(max_bytes=1000)
    report = build_memory_pipeline(seed=seed, emitter=sink, attachments=policy).run_once()
    assert report.attachments_stripped == 1
    assert report.emitted == 1
    email = sink.events[0].email
    assert email.attachments == []
    assert len(email.stripped_attachments) == 1
    assert email.stripped_attachments[0].reason.value == "oversize"


def test_over_msg_cap_dead_lettered(sink):
    # The B1 message-level size guard runs BEFORE extraction (Pipeline._process),
    # so a message whose total wire size busts max_message_bytes is dead-lettered
    # without ever reaching the attachment cap logic.
    seed = {S: [SeedEmail("big2", large_attachment_raw(2_000_000))]}
    report = build_memory_pipeline(seed=seed, emitter=sink, max_message_bytes=1000).run_once()
    assert report.dead_lettered == 1
    assert report.emitted == 0


def test_allowlist_blocks_non_listed(sink):
    # Legacy (non-policy) allowlist path: a disallowed type RAISES
    # AttachmentBlockedError (a PermanentError) -> the whole message is quarantined to DLQ.
    seed = {S: [SeedEmail("att1", large_attachment_raw(100))]}
    extractor = MimeExtractor(allowlist=frozenset({"application/pdf"}))
    report = build_memory_pipeline(seed=seed, emitter=sink, extractor=extractor).run_once()
    assert report.dead_lettered == 1
    assert report.emitted == 0


def test_allowlist_case_insensitive():
    # check_allowlist lowercases attachment.content_type before comparing
    # (extract/safety.py) -- unit-level proof, independent of the stdlib email
    # package's own content-type lowering (get_content_type() always lowercases,
    # which would otherwise mask this specific code path in a full-pipeline test).
    attachment = Attachment(filename="invoice.pdf", content_type="application/PDF")
    result = check_allowlist(attachment, frozenset({"application/pdf"}))
    assert result.verdict == ScanVerdict.allow


def test_huge_attachment_no_oom(sink):
    # 30 MB attachment; per-class cap raised above it so it is KEPT, proving the
    # streaming decode path (extract/streaming.py chunks in 64 KiB pieces) handles
    # it without buffering the whole payload, and the pipeline completes cleanly.
    seed = {S: [SeedEmail("huge1", large_attachment_raw(30_000_000))]}
    policy = AttachmentPolicy(real=AttachmentRule(max_bytes=40_000_000))
    report = build_memory_pipeline(seed=seed, emitter=sink, attachments=policy).run_once()
    assert report.emitted == 1
    email = sink.events[0].email
    assert len(email.attachments) == 1
    assert email.attachments[0].size_bytes == 30_000_000
