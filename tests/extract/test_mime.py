from mailflow.core.models import Direction
from mailflow.extract.mime import MimeExtractor

PLAIN = (
    b"Message-ID: <abc.1@example.com>\r\n"
    b"From: Alice <alice@partner.com>\r\n"
    b"To: ops@acme.com\r\n"
    b"Subject: Quote request\r\n"
    b"Date: Mon, 09 Jun 2026 10:00:00 +0000\r\n"
    b"\r\n"
    b"Hello, please send a quote.\r\n"
)

MULTIPART = (
    b"Message-ID: <m2@example.com>\r\n"
    b"From: Bob <bob@partner.com>\r\n"
    b"To: ops@acme.com\r\n"
    b"Subject: With attachment\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
    b"--B\r\n"
    b"Content-Type: text/plain\r\n\r\nBody text here\r\n"
    b"--B\r\n"
    b"Content-Type: application/pdf\r\n"
    b'Content-Disposition: attachment; filename="q.pdf"\r\n\r\n'
    b"PDFBYTES\r\n"
    b"--B\r\n"
    b"Content-Type: image/png\r\n"
    b"Content-ID: <logo>\r\n"
    b"Content-Disposition: inline\r\n\r\n"
    b"PNGBYTES\r\n"
    b"--B--\r\n"
)


def _extract(raw: bytes, mailbox="ops@acme.com:Inbox"):
    return MimeExtractor().extract_bytes(
        raw, provider="memory", provider_message_id="m", stream_id=mailbox,
        watched_mailbox="ops@acme.com",
    )


def test_extracts_headers_addresses_and_body():
    ce = _extract(PLAIN)
    assert ce.subject == "Quote request"
    assert ce.from_.address == "alice@partner.com"
    assert ce.from_.name == "Alice"
    assert ce.to[0].address == "ops@acme.com"
    assert "send a quote" in ce.body_text
    assert ce.message_id == "<abc.1@example.com>"
    assert ce.message_id_present is True
    assert ce.message_id_trusted is True
    assert ce.raw_headers["subject"] == ["Quote request"]


def test_inbound_direction_when_sender_is_not_the_watched_mailbox():
    ce = _extract(PLAIN)
    assert ce.direction is Direction.inbound


def test_outbound_direction_when_sender_is_the_watched_mailbox():
    raw = PLAIN.replace(b"alice@partner.com", b"ops@acme.com")
    ce = _extract(raw)
    assert ce.direction is Direction.outbound


def test_separates_real_attachment_from_inline_media():
    ce = _extract(MULTIPART)
    reals = [a for a in ce.attachments if not a.is_inline]
    inlines = [a for a in ce.attachments if a.is_inline]
    assert len(reals) == 1 and reals[0].filename == "q.pdf"
    assert reals[0].content_hash and len(reals[0].content_hash) == 64
    assert len(inlines) == 1 and inlines[0].content_id == "<logo>"
    assert "Body text here" in ce.body_text


def test_canonical_id_present_even_without_message_id():
    raw = PLAIN.replace(b"Message-ID: <abc.1@example.com>\r\n", b"")
    ce = _extract(raw)
    assert ce.canonical_id  # always present
    assert ce.message_id is None
    assert ce.message_id_present is False
    assert ce.message_id_trusted is False
