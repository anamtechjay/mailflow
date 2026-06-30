"""Attachment streaming + fail-closed per-attachment byte cap (V1 fast-follow)."""

from __future__ import annotations

import hashlib
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy

import pytest

from mailflow.extract.streaming import (
    AttachmentTooLargeError,
    AttachmentUnreadableError,
    digest_and_size,
    iter_decoded,
)


def _attachment_part(
    data: bytes, *, filename: str = "report.pdf", ctype: str = "application/pdf"
) -> EmailMessage:
    m = EmailMessage()
    m["Message-ID"] = "<a.1@example.com>"
    m["From"] = "alice@partner.com"
    m["To"] = "ops@acme.com"
    m["Subject"] = "with attachment"
    m.set_content("see attached")
    maintype, subtype = ctype.split("/", 1)
    m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    parsed = message_from_bytes(m.as_bytes(), policy=default_policy)
    assert isinstance(parsed, EmailMessage)
    return next(p for p in parsed.walk() if p.get_filename() == filename)


def test_iter_decoded_streams_in_chunks_and_round_trips() -> None:
    data = bytes(range(256)) * 40  # 10 KiB, base64-encoded by add_attachment
    part = _attachment_part(data)
    chunks = list(iter_decoded(part, chunk_size=1024))
    assert len(chunks) > 1  # genuinely chunked, not one buffer
    assert b"".join(chunks) == data  # lossless round-trip


def test_digest_and_size_matches_sha256_and_length() -> None:
    data = b"hello attachment world" * 100
    part = _attachment_part(data)
    digest, size = digest_and_size(part, cap=10_000_000)
    assert size == len(data)
    assert digest == hashlib.sha256(data).hexdigest()


def test_over_cap_attachment_fails_closed() -> None:
    part = _attachment_part(b"x" * 5000)
    with pytest.raises(AttachmentTooLargeError):
        digest_and_size(part, cap=1024)


def test_invalid_base64_is_unreadable_fail_closed() -> None:
    raw = (
        b"Message-ID: <bad.1@example.com>\r\n"
        b"From: alice@partner.com\r\n"
        b"To: ops@acme.com\r\n"
        b"Subject: bad\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="x.bin"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"A\r\n"  # 1 base64 data char -> binascii.Error (invalid length)
    )
    parsed = message_from_bytes(raw, policy=default_policy)
    assert isinstance(parsed, EmailMessage)
    part = next(p for p in parsed.walk() if p.get_filename() == "x.bin")
    with pytest.raises(AttachmentUnreadableError):
        digest_and_size(part, cap=10_000_000)
