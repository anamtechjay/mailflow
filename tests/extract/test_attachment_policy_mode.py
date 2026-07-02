"""MimeExtractor policy mode: per-class strip dispatch + classification (Task 3).

When a MimeExtractor is given an AttachmentPolicy, a part that violates its
per-class rule is STRIPPED (omitted from `attachments`, no blob stored) and
recorded in `CleanEmail.stripped_attachments` — never raised. `attachment_policy
is None` keeps today's raise->DLQ behaviour byte-for-byte.
"""

from __future__ import annotations

import base64

import pytest

from mailflow.core.models import Attachment, ScanResult, ScanVerdict, StripReason
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule
from mailflow.extract.safety import AttachmentBlockedError
from mailflow.stores.memory import InMemoryBlobStore

PDF = b"%PDF-1.4 hello pdf payload"
PNG = b"\x89PNG\r\n\x1a\n png pixel bytes"


def _b64(data: bytes) -> bytes:
    return base64.b64encode(data)


def _msg(*parts: bytes) -> bytes:
    body = (
        b"Message-ID: <m.1@example.com>\r\n"
        b"From: alice@partner.com\r\n"
        b"To: ops@acme.com\r\n"
        b"Subject: hi\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUND"\r\n'
        b"\r\n"
        b"--BOUND\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"hello body\r\n"
    )
    for p in parts:
        body += b"--BOUND\r\n" + p
    body += b"--BOUND--\r\n"
    return body


def _real(data: bytes, ctype: bytes = b"application/pdf", name: bytes = b"report.pdf") -> bytes:
    return (
        b"Content-Type: " + ctype + b'; name="' + name + b'"\r\n'
        b'Content-Disposition: attachment; filename="' + name + b'"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _b64(data) + b"\r\n"
    )


def _inline(data: bytes, ctype: bytes = b"image/png", name: bytes = b"logo.png") -> bytes:
    # Named-CID part with NO Content-Disposition -> inline_for_policy must be True.
    return (
        b"Content-Type: " + ctype + b'; name="' + name + b'"\r\n'
        b"Content-ID: <logo1>\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _b64(data) + b"\r\n"
    )


def _attach_with_cid(data: bytes, ctype: bytes = b"image/png", name: bytes = b"logo.png") -> bytes:
    # disposition=attachment AND a Content-ID -> must route to the REAL rule.
    return (
        b"Content-Type: " + ctype + b'; name="' + name + b'"\r\n'
        b"Content-ID: <logo2>\r\n"
        b'Content-Disposition: attachment; filename="' + name + b'"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _b64(data) + b"\r\n"
    )


def _bad_base64(disposition_attachment: bool) -> bytes:
    headers = (
        b'Content-Type: application/pdf; name="bad.pdf"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
    )
    if disposition_attachment:
        headers += b'Content-Disposition: attachment; filename="bad.pdf"\r\n'
    else:
        headers += b"Content-ID: <bad1>\r\n"
    return headers + b"\r\nA\r\n"  # single base64 char -> binascii.Error -> unreadable


class _BlockAll:
    def scan(self, attachment: Attachment) -> ScanResult:
        return ScanResult(verdict=ScanVerdict.block, reason="quarantined by test scanner")


def _run(ext: MimeExtractor, raw: bytes) -> tuple[object, InMemoryBlobStore]:
    store = InMemoryBlobStore()
    ce = ext.extract_bytes(
        raw,
        provider="memory",
        provider_message_id="m",
        stream_id="ops@acme.com/Inbox",
        watched_mailbox="ops@acme.com",
        blob_store=store,
    )
    return ce, store


# --- one strip test per reason x {inline, real} -----------------------------


def test_strip_not_allowlisted_real() -> None:
    policy = AttachmentPolicy(real=AttachmentRule(allowlist={"text/plain"}))
    ce, store = _run(MimeExtractor(attachment_policy=policy), _msg(_real(PDF)))
    assert ce.attachments == []
    assert len(ce.stripped_attachments) == 1
    s = ce.stripped_attachments[0]
    assert s.reason is StripReason.not_allowlisted
    assert s.is_inline is False
    assert s.content_type == "application/pdf"
    assert s.size_bytes == 0
    assert "hello body" in ce.body_text
    assert store._blobs == {}  # nothing persisted for the stripped part


def test_strip_not_allowlisted_inline() -> None:
    policy = AttachmentPolicy(inline=AttachmentRule(allowlist={"text/plain"}))
    ce, store = _run(MimeExtractor(attachment_policy=policy), _msg(_inline(PNG)))
    assert ce.attachments == []
    s = ce.stripped_attachments[0]
    assert s.reason is StripReason.not_allowlisted
    assert s.is_inline is True
    assert store._blobs == {}


def test_strip_oversize_real() -> None:
    policy = AttachmentPolicy(real=AttachmentRule(max_bytes=4))
    ce, store = _run(MimeExtractor(attachment_policy=policy), _msg(_real(PDF)))
    s = ce.stripped_attachments[0]
    assert ce.attachments == []
    assert s.reason is StripReason.oversize
    assert s.is_inline is False
    assert store._blobs == {}


def test_strip_oversize_inline() -> None:
    policy = AttachmentPolicy(inline=AttachmentRule(max_bytes=4))
    ce, store = _run(MimeExtractor(attachment_policy=policy), _msg(_inline(PNG)))
    s = ce.stripped_attachments[0]
    assert ce.attachments == []
    assert s.reason is StripReason.oversize
    assert s.is_inline is True
    assert store._blobs == {}


def test_strip_unreadable_real() -> None:
    ce, store = _run(MimeExtractor(attachment_policy=AttachmentPolicy()), _msg(_bad_base64(True)))
    s = ce.stripped_attachments[0]
    assert ce.attachments == []
    assert s.reason is StripReason.unreadable
    assert s.is_inline is False
    assert store._blobs == {}


def test_strip_unreadable_inline() -> None:
    ce, store = _run(MimeExtractor(attachment_policy=AttachmentPolicy()), _msg(_bad_base64(False)))
    s = ce.stripped_attachments[0]
    assert ce.attachments == []
    assert s.reason is StripReason.unreadable
    assert s.is_inline is True
    assert store._blobs == {}


def test_strip_scanner_real() -> None:
    policy = AttachmentPolicy(real=AttachmentRule(scanner=_BlockAll()))
    ce, store = _run(MimeExtractor(attachment_policy=policy), _msg(_real(PDF)))
    s = ce.stripped_attachments[0]
    assert ce.attachments == []
    assert s.reason is StripReason.scanner
    assert s.is_inline is False
    assert s.size_bytes == len(PDF)  # size known by the time the scanner runs
    assert store._blobs == {}  # scanner ran AFTER digest but blob is NOT written


def test_strip_scanner_inline() -> None:
    policy = AttachmentPolicy(inline=AttachmentRule(scanner=_BlockAll()))
    ce, store = _run(MimeExtractor(attachment_policy=policy), _msg(_inline(PNG)))
    s = ce.stripped_attachments[0]
    assert ce.attachments == []
    assert s.reason is StripReason.scanner
    assert s.is_inline is True
    assert s.size_bytes == len(PNG)
    assert store._blobs == {}


# --- classification ----------------------------------------------------------


def test_named_cid_logo_routes_to_inline_rule() -> None:
    # inline rule blocks image/png; real rule allows it. If the part routed to
    # `real` it would be kept -> being stripped proves it routed to `inline`.
    policy = AttachmentPolicy(
        real=AttachmentRule(allowlist={"image/png"}),
        inline=AttachmentRule(allowlist={"application/pdf"}),
    )
    ce, _ = _run(MimeExtractor(attachment_policy=policy), _msg(_inline(PNG)))
    assert ce.attachments == []
    assert ce.stripped_attachments[0].is_inline is True
    assert ce.stripped_attachments[0].reason is StripReason.not_allowlisted


def test_attachment_disposition_with_cid_routes_to_real_rule() -> None:
    # real rule blocks image/png; inline rule allows everything. Stripped ->
    # routed to `real` despite carrying a Content-ID.
    policy = AttachmentPolicy(
        real=AttachmentRule(allowlist={"application/pdf"}),
        inline=AttachmentRule(),  # empty allowlist = allow-all
    )
    ce, _ = _run(MimeExtractor(attachment_policy=policy), _msg(_attach_with_cid(PNG)))
    assert ce.attachments == []
    assert ce.stripped_attachments[0].is_inline is False
    assert ce.stripped_attachments[0].reason is StripReason.not_allowlisted


def test_per_class_difference_pdf_allowed_real_stripped_inline() -> None:
    policy = AttachmentPolicy(
        real=AttachmentRule(allowlist={"application/pdf"}),
        inline=AttachmentRule(allowlist={"image/png"}),
    )
    raw = _msg(_real(PDF), _inline(PDF, ctype=b"application/pdf", name=b"inlinedoc.pdf"))
    ce, store = _run(MimeExtractor(attachment_policy=policy), raw)
    # real pdf kept + blob stored
    assert len(ce.attachments) == 1
    assert ce.attachments[0].content_type == "application/pdf"
    assert ce.attachments[0].storage_ref
    assert len(store._blobs) == 1
    # inline pdf stripped, not_allowlisted, is_inline True
    assert len(ce.stripped_attachments) == 1
    assert ce.stripped_attachments[0].is_inline is True
    assert ce.stripped_attachments[0].reason is StripReason.not_allowlisted


# --- back-compat (None path still raises -> DLQ) -----------------------------


def test_none_policy_still_raises_on_blocked() -> None:
    ext = MimeExtractor(allowlist=frozenset({"text/plain"}))
    with pytest.raises(AttachmentBlockedError):
        _run(ext, _msg(_real(PDF)))


def test_none_policy_still_raises_on_oversize() -> None:
    from mailflow.extract.streaming import AttachmentTooLargeError

    ext = MimeExtractor(max_attachment_bytes=4)
    with pytest.raises(AttachmentTooLargeError):
        _run(ext, _msg(_real(PDF)))


# --- mutual exclusion --------------------------------------------------------


def test_mutual_exclusion_allowlist_and_policy() -> None:
    with pytest.raises(ValueError):
        MimeExtractor(allowlist=frozenset({"x"}), attachment_policy=AttachmentPolicy())


def test_mutual_exclusion_scanner_and_policy() -> None:
    with pytest.raises(ValueError):
        MimeExtractor(scanner=_BlockAll(), attachment_policy=AttachmentPolicy())


def test_mutual_exclusion_max_bytes_and_policy() -> None:
    with pytest.raises(ValueError):
        MimeExtractor(max_attachment_bytes=10, attachment_policy=AttachmentPolicy())
