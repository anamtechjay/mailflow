"""F03 — Envelope parse (golden corpus). See docs/qa-partA-coverage.md.

NOTE (adjustment, logged in the Phase-1 report): `MimeEnvelopeParser.parse_envelope`
(extract/envelope.py) never populates `Envelope.date_utc` at all — it is a
cheap, pre-filter parse (spec §7.3) that does not touch the Date header. Only the
full extractor (`MimeExtractor.extract_bytes`, spec §7.6) parses `Date` with a
try/except that falls back to `None` on a garbage value. So the "garbage/absent
Date" edge case is tested against the extractor (where date parsing + the
fail-to-None behavior actually lives), not against `parse_envelope`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from email.message import EmailMessage
from email.policy import default as default_policy

from mailflow.core.models import Cursor, RawMessage, StreamRef
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor

from tests._harness.corpus import TRICKY
from tests._harness.email_builder import Email

TENANT = "acme"
STREAM = StreamRef(mailbox="me@acme.com", folder="inbox")


def _rawmsg(raw_bytes: bytes, provider_message_id: str = "m1") -> RawMessage:
    return RawMessage(
        provider="memory",
        provider_message_id=provider_message_id,
        stream=STREAM,
        size_bytes=len(raw_bytes),
        received_at=datetime.now(timezone.utc),
        cursor=Cursor(value="x", order=1),
        raw_bytes=raw_bytes,
    )


def test_missing_from_fallback():
    env = MimeEnvelopeParser().parse_envelope(_rawmsg(TRICKY["missing_from"]), TENANT)
    assert env.from_.address == ""  # fallback Recipient(), no crash


def test_absent_message_id_parses():
    env = MimeEnvelopeParser().parse_envelope(_rawmsg(TRICKY["no_message_id"]), TENANT)
    assert env.message_id is None
    assert env.message_id_present is False
    assert env.canonical_id  # still built (via stable_hash fallback)


def test_folded_header():
    # A long Subject gets line-folded on the wire by EmailMessage.as_bytes(); the
    # parser must reassemble it back to the original unfolded value.
    long_subject = "A" * 300
    raw_bytes = Email(msg_id="T-FOLD", subject=long_subject, body="hi").as_rfc822()
    assert b"\r\n " in raw_bytes or b"\n " in raw_bytes  # sanity: it really did fold
    env = MimeEnvelopeParser().parse_envelope(_rawmsg(raw_bytes), TENANT)
    assert env.subject == long_subject


def test_encoded_word_subject():
    unicode_subject = "Héllo Wörld – caféré"
    raw_bytes = Email(msg_id="T-ENC", subject=unicode_subject, body="hi").as_rfc822()
    assert b"=?utf-8?" in raw_bytes.lower()  # sanity: really RFC2047-encoded on the wire
    env = MimeEnvelopeParser().parse_envelope(_rawmsg(raw_bytes), TENANT)
    assert env.subject == unicode_subject


@pytest.mark.parametrize("date_header", ["not-a-date-string", None])
def test_bad_date_is_none(date_header):
    msg = EmailMessage(policy=default_policy)
    msg["From"] = "a@partner.com"
    msg["To"] = "me@acme.com"
    msg["Subject"] = "date test"
    msg["Message-ID"] = "<date1@partner.com>"
    if date_header is not None:
        msg["Date"] = date_header
    msg.set_content("hi")
    raw_bytes = msg.as_bytes()

    clean = MimeExtractor().extract_bytes(
        raw_bytes, provider="memory", provider_message_id="m1",
        stream_id=STREAM.key, watched_mailbox=STREAM.mailbox,
    )
    assert clean.date_utc is None


def test_multiple_from():
    # Two From header lines on the wire (the default EmailMessage policy caps From
    # at 1 and raises on assignment, so build the raw bytes by hand) -- the first
    # one wins.
    raw_bytes = (
        b"From: first@partner.com\r\n"
        b"From: second@partner.com\r\n"
        b"To: me@acme.com\r\n"
        b"Subject: multi from\r\n"
        b"Message-ID: <mf@partner.com>\r\n"
        b"\r\n"
        b"hi\r\n"
    )

    env = MimeEnvelopeParser().parse_envelope(_rawmsg(raw_bytes), TENANT)
    assert env.from_.address == "first@partner.com"
