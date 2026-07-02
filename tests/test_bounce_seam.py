"""is_auto_submitted / is_bounce seam on the Envelope (MIME path) + ListMailFilter rewire."""

from __future__ import annotations

from datetime import datetime, timezone

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Cursor, Decision, Envelope, RawMessage, StreamRef
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.filters.deterministic import ListMailFilter

CTX = FilterContext(tenant="t")
STREAM = StreamRef(mailbox="ops@acme.com")


def _raw_msg(raw: bytes) -> RawMessage:
    return RawMessage(
        provider="memory", provider_message_id="m1", stream=STREAM,
        size_bytes=len(raw), received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        cursor=Cursor(value="1", order=1), raw_bytes=raw,
    )


def test_envelope_auto_submitted_seam() -> None:
    raw = (b"Message-ID: <a@x>\r\nFrom: s@x.com\r\nTo: ops@acme.com\r\n"
           b"Auto-Submitted: auto-generated\r\nSubject: hi\r\n\r\nbody")
    env = MimeEnvelopeParser().parse_envelope(_raw_msg(raw), "t")
    assert env.auto_submitted == "auto-generated"   # legacy string preserved
    assert env.is_auto_submitted is True
    assert env.is_bounce is False


def test_envelope_bounce_from_daemon_and_null_return_path() -> None:
    raw = (b"Message-ID: <a@x>\r\nFrom: MAILER-DAEMON@mx.acme.com\r\n"
           b"Return-Path: <>\r\nTo: ops@acme.com\r\nSubject: failure\r\n\r\nx")
    env = MimeEnvelopeParser().parse_envelope(_raw_msg(raw), "t")
    assert env.is_bounce is True


def test_list_mail_filter_uses_boolean_seam() -> None:
    drop_env = Envelope(canonical_id="c", provider="memory", provider_message_id="m",
                        stream=STREAM, is_auto_submitted=True)
    keep_env = Envelope(canonical_id="c", provider="memory", provider_message_id="m",
                        stream=STREAM, is_auto_submitted=False)
    assert ListMailFilter().evaluate(drop_env, CTX).decision is Decision.drop
    assert ListMailFilter().evaluate(keep_env, CTX).decision is Decision.uncertain


from mailflow.extract.mime import MimeExtractor  # noqa: E402


def test_clean_email_bounce_delivery_status_report() -> None:
    raw = (b"Message-ID: <a@x>\r\nFrom: bounce-handler@mailer.acme.com\r\n"
           b"To: ops@acme.com\r\nSubject: Delivery Status\r\n"
           b'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n'
           b"\r\n--b\r\nContent-Type: text/plain\r\n\r\nfailed\r\n--b--\r\n")
    ce = MimeExtractor().extract_bytes(
        raw, provider="memory", provider_message_id="m1",
        stream_id="ops@acme.com", watched_mailbox="ops@acme.com",
    )
    assert ce.is_bounce is True            # via the delivery-status content-type
    assert ce.is_auto_submitted is False


def test_clean_email_auto_submitted_seam() -> None:
    raw = (b"Message-ID: <a@x>\r\nFrom: s@x.com\r\nTo: ops@acme.com\r\n"
           b"Auto-Submitted: auto-generated\r\nSubject: hi\r\n\r\nbody")
    ce = MimeExtractor().extract_bytes(
        raw, provider="memory", provider_message_id="m1",
        stream_id="ops@acme.com", watched_mailbox="ops@acme.com",
    )
    assert ce.is_auto_submitted is True
    assert ce.auto_submitted == "auto-generated"   # legacy string preserved


from mailflow.core.events import EmailEvent  # noqa: E402


def test_classification_seam_survives_onto_emitted_wire_event() -> None:
    # A bounce (delivery-status report) extracted, then wrapped + serialized as
    # the wire EmailEvent — proves the booleans are stored wire fields, not
    # dropped between the extractor boundary and the emitted payload.
    raw = (b"Message-ID: <a@x>\r\nFrom: bounce-handler@mailer.acme.com\r\n"
           b"To: ops@acme.com\r\nSubject: Delivery Status\r\n"
           b'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n'
           b"\r\n--b\r\nContent-Type: text/plain\r\n\r\nfailed\r\n--b--\r\n")
    ce = MimeExtractor().extract_bytes(
        raw, provider="memory", provider_message_id="m1",
        stream_id="ops@acme.com", watched_mailbox="ops@acme.com",
    )
    dumped = EmailEvent(tenant="t", ordering_key="ops@acme.com", email=ce).model_dump()
    assert dumped["email"]["is_bounce"] is True
    assert dumped["email"]["is_auto_submitted"] is False
