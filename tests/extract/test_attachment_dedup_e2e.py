"""Same attachment in two distinct messages is content-addressed and stored once."""

from __future__ import annotations

from email.message import EmailMessage

from mailflow.extract.mime import MimeExtractor
from mailflow.stores.memory import InMemoryBlobStore

PDF = b"%PDF-1.4 the very same bytes in both messages"


def _raw(mid: str, subject: str) -> bytes:
    m = EmailMessage()
    m["Message-ID"] = f"<{mid}@example.com>"
    m["From"] = "alice@partner.com"
    m["To"] = "ops@acme.com"
    m["Subject"] = subject
    m.set_content("see attached")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="report.pdf")
    return m.as_bytes()


def test_same_attachment_two_messages_stored_once() -> None:
    store = InMemoryBlobStore()
    ext = MimeExtractor()
    common = dict(
        provider="memory",
        stream_id="ops@acme.com/Inbox",
        watched_mailbox="ops@acme.com",
        blob_store=store,
    )
    a = ext.extract_bytes(_raw("a1", "first"), provider_message_id="a1", **common)
    b = ext.extract_bytes(_raw("b1", "second"), provider_message_id="b1", **common)
    assert a.attachments[0].content_hash == b.attachments[0].content_hash
    assert a.attachments[0].storage_ref == b.attachments[0].storage_ref
    assert len(store._blobs) == 1  # written once, not overwritten twice
