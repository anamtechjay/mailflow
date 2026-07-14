"""MIME-7 — a hostile attachment filename (path traversal) must be sanitized to a
safe basename before it reaches `CleanEmail.attachments[].filename`.

A message with `Content-Disposition: attachment; filename="../../etc/passwd"` must not
surface that path to the consumer app — a naive `open(att.filename, "wb")` would then
write outside the intended directory. The extractor strips any directory component and
path-traversal, keeping only the final name segment.
"""

from __future__ import annotations

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

from tests._harness.fakes import build_memory_pipeline
from tests._harness.reliability import MIME_ADVERSARIAL

S = StreamRef(mailbox="me@acme.com", folder="inbox")


def _hostile_raw() -> bytes:
    return next(e for e in MIME_ADVERSARIAL if e["name"] == "hostile_attachment_filename")["raw"]


def test_hostile_filename_is_sanitized_to_basename(sink):
    seed = {S: [SeedEmail("hostile1", _hostile_raw())]}
    build_memory_pipeline(seed=seed, emitter=sink).run_once()

    assert len(sink.events) == 1
    atts = sink.events[0].email.attachments
    assert len(atts) == 1
    name = atts[0].filename
    # No directory separators, no traversal, no absolute path — just the leaf name.
    assert "/" not in name and "\\" not in name
    assert ".." not in name
    assert name == "passwd"
