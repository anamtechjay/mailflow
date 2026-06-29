from mailflow.extract.mime import MimeExtractor

HTML_ONLY = (
    b"Message-ID: <html.1@example.com>\r\n"
    b"From: Alice <alice@partner.com>\r\n"
    b"To: ops@acme.com\r\n"
    b"Subject: HTML only\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<html><body><p>Hello,</p><p>please send a <b>quote</b>.</p></body></html>\r\n"
)


def _extract(raw: bytes, *, thread_key: str = ""):
    return MimeExtractor().extract_bytes(
        raw,
        provider="memory",
        provider_message_id="m",
        stream_id="ops@acme.com/Inbox",
        watched_mailbox="ops@acme.com",
        thread_key=thread_key,
    )


def test_html_only_body_populates_body_text():
    ce = _extract(HTML_ONLY)
    assert ce.body_html  # html captured
    assert ce.body_text  # non-empty, derived from html
    assert "please send a quote" in ce.body_text
