"""MIME-2 — a message declaring an unknown/bogus charset must still be delivered, not
lost to an uncaught `LookupError`.

`email`'s `part.get_content()` raises `LookupError` when the part's declared charset is
not a registered codec (e.g. `x-totally-made-up`, or legacy `unknown-8bit`). That escapes
the extractor, is misclassified as a generic (transient) failure, and the mail retries
forever or is dropped — legitimate foreign-language mail is lost. The extractor must fall
back to a permissive decode so the message is emitted with a best-effort body.
"""

from __future__ import annotations

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

from tests._harness.fakes import build_memory_pipeline
from tests._harness.reliability import MIME_ADVERSARIAL

S = StreamRef(mailbox="me@acme.com", folder="inbox")


def _unknown_charset_raw() -> bytes:
    return next(e for e in MIME_ADVERSARIAL if e["name"] == "unknown_charset")["raw"]


def test_unknown_charset_still_delivers_with_body(sink):
    seed = {S: [SeedEmail("uc1", _unknown_charset_raw())]}
    report = build_memory_pipeline(seed=seed, emitter=sink).run_once()

    assert report.emitted == 1          # delivered, not dead-lettered, not crashed
    assert report.dead_lettered == 0
    body = sink.events[0].email.body_text
    assert "charset nobody registered" in body   # best-effort ASCII survived the fallback
