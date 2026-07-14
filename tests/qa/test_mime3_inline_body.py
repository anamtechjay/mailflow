"""MIME-3 — a text body sent with `Content-Disposition: inline` (mutt / Mailman style),
with no filename and no Content-ID, must populate `body_text`, not be misclassified as an
anonymous attachment that leaves the app with an empty email.
"""

from __future__ import annotations

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

from tests._harness.fakes import build_memory_pipeline
from tests._harness.reliability import MIME_ADVERSARIAL

S = StreamRef(mailbox="me@acme.com", folder="inbox")


def _inline_raw() -> bytes:
    return next(e for e in MIME_ADVERSARIAL if e["name"] == "inline_disposition_text")["raw"]


def test_inline_disposition_text_becomes_body(sink):
    seed = {S: [SeedEmail("inl1", _inline_raw())]}
    report = build_memory_pipeline(seed=seed, emitter=sink).run_once()

    assert report.emitted == 1
    email = sink.events[0].email
    assert "Content-Disposition: inline" in email.body_text or "mutt/Mailman" in email.body_text
    assert email.body_text.strip() != ""       # NOT an empty email
    assert email.attachments == []              # NOT surfaced as an attachment
