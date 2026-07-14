"""Scenario — large messages. See docs/qa-partA-coverage.md ("Scenarios & invariants").

Mirrors F08's `test_over_attach_cap_stripped` / `test_over_msg_cap_dead_lettered`
(`tests/qa/test_f08_attachments.py`) but at the actual 30 MB / 60 MB scale the
scenario map calls for, rather than F08's smaller stand-ins. Marked `slow`
since both allocate large in-memory buffers.
"""

from __future__ import annotations

import pytest

from mailflow.core.models import StreamRef
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule
from mailflow.providers.memory import SeedEmail

from tests._harness.corpus import large_attachment_raw
from tests._harness.fakes import build_memory_pipeline

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


@pytest.mark.slow
def test_30mb_attachment_stripped(sink):
    # Per-attachment cap (20 MB) is below the 30 MB attachment -> the policy
    # path STRIPS the part but still emits the message (extract/mime.py's
    # `_process_policy` never raises on an oversize attachment).
    seed = {S: [SeedEmail("big30", large_attachment_raw(30_000_000))]}
    policy = AttachmentRule(max_bytes=20_000_000)
    report = build_memory_pipeline(
        seed=seed, emitter=sink, attachments=policy, max_message_bytes=100_000_000,
    ).run_once()

    assert report.attachments_stripped == 1
    assert report.emitted == 1
    assert report.dead_lettered == 0
    email = sink.events[0].email
    assert email.attachments == []
    assert len(email.stripped_attachments) == 1


@pytest.mark.slow
def test_60mb_message_dead_lettered(sink):
    # max_message_bytes (60 MB) is set BELOW the actual wire size of a message
    # carrying a 60 MB attachment payload (base64 + MIME framing inflates it
    # past 60 MB) -- the B1 message-level size guard runs before extraction and
    # dead-letters the whole message without ever reaching attachment policy.
    seed = {S: [SeedEmail("big60", large_attachment_raw(60_000_000))]}
    report = build_memory_pipeline(seed=seed, emitter=sink, max_message_bytes=60_000_000).run_once()

    assert report.dead_lettered == 1
    assert report.emitted == 0
