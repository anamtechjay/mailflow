from datetime import datetime, timezone

from mailflow.core.models import Cursor, RawMessage, StreamRef
from mailflow.extract.envelope import MimeEnvelopeParser

RAW = (
    b"Message-ID: <e1@example.com>\r\n"
    b"From: Carol <carol@partner.com>\r\n"
    b"To: ops@acme.com, sales@acme.com\r\n"
    b"Subject: Newsletter\r\n"
    b"List-Id: <news.partner.com>\r\n"
    b"Auto-Submitted: auto-generated\r\n"
    b"\r\n"
    b"This is the body and it is fairly long so the snippet should be truncated nicely.\r\n"
)


def _raw_message() -> RawMessage:
    return RawMessage(
        provider="memory",
        provider_message_id="m1",
        stream=StreamRef(mailbox="ops@acme.com", folder="Inbox"),
        size_bytes=len(RAW),
        received_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        cursor=Cursor(value="c1", order=1),
        raw_bytes=RAW,
    )


def test_parses_envelope_fields():
    env = MimeEnvelopeParser().parse_envelope(_raw_message(), tenant="acme")
    assert env.subject == "Newsletter"
    assert env.from_.address == "carol@partner.com"
    assert [r.address for r in env.to] == ["ops@acme.com", "sales@acme.com"]
    assert env.list_id == "<news.partner.com>"
    assert env.auto_submitted == "auto-generated"
    assert env.canonical_id == "<e1@example.com>"
    assert env.message_id_trusted is True


def test_snippet_is_bounded():
    env = MimeEnvelopeParser(snippet_chars=20).parse_envelope(_raw_message(), tenant="acme")
    assert len(env.snippet) <= 20
    assert env.snippet.startswith("This is the body")


def test_envelope_keeps_provider_metadata():
    env = MimeEnvelopeParser().parse_envelope(_raw_message(), tenant="acme")
    assert env.provider == "memory"
    assert env.provider_message_id == "m1"
    assert env.stream.key == "ops@acme.com:Inbox"
