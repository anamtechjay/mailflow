"""Attachment safety seam: no-op default + allowlist + scanner hook (V1 fast-follow)."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from mailflow.core.models import Attachment, ScanResult, ScanVerdict
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.safety import (
    AttachmentBlockedError,
    NoOpAttachmentScanner,
    check_allowlist,
)
from mailflow.stores.memory import InMemoryBlobStore

PDF = b"%PDF-1.4 hello"


def _raw_with_pdf() -> bytes:
    m = EmailMessage()
    m["Message-ID"] = "<s.1@example.com>"
    m["From"] = "alice@partner.com"
    m["To"] = "ops@acme.com"
    m["Subject"] = "doc"
    m.set_content("see attached")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="report.pdf")
    return m.as_bytes()


def _extract(extractor: MimeExtractor, raw: bytes):
    return extractor.extract_bytes(
        raw,
        provider="memory",
        provider_message_id="m",
        stream_id="ops@acme.com/Inbox",
        watched_mailbox="ops@acme.com",
        blob_store=InMemoryBlobStore(),
    )


def test_noop_scanner_allows_everything() -> None:
    result = NoOpAttachmentScanner().scan(Attachment(filename="x.exe"))
    assert result.verdict is ScanVerdict.allow


def test_empty_allowlist_allows() -> None:
    att = Attachment(filename="x.exe", content_type="application/x-msdownload")
    assert check_allowlist(att, frozenset()).verdict is ScanVerdict.allow


def test_allowlist_blocks_non_listed_type() -> None:
    att = Attachment(filename="x.exe", content_type="application/x-msdownload")
    assert check_allowlist(att, frozenset({"pdf", "application/pdf"})).verdict is ScanVerdict.block


def test_default_extractor_keeps_attachment() -> None:
    ce = _extract(MimeExtractor(), _raw_with_pdf())
    assert len(ce.attachments) == 1
    assert ce.attachments[0].content_type == "application/pdf"


def test_extractor_with_allowlist_blocks_message() -> None:
    ext = MimeExtractor(allowlist=frozenset({"text/plain"}))
    with pytest.raises(AttachmentBlockedError):
        _extract(ext, _raw_with_pdf())


class _BlockAll:
    def scan(self, attachment: Attachment) -> ScanResult:
        return ScanResult(verdict=ScanVerdict.block, reason="quarantined by test scanner")


def test_extractor_with_blocking_scanner_blocks_message() -> None:
    ext = MimeExtractor(scanner=_BlockAll())
    with pytest.raises(AttachmentBlockedError):
        _extract(ext, _raw_with_pdf())


def test_allowlist_rejects_before_cap_decode() -> None:
    # report.pdf is over this tiny cap AND not in the allowlist. Allowlist runs first,
    # so we get AttachmentBlockedError (not AttachmentTooLargeError) — proving the
    # disallowed type is rejected without paying for the full decode.
    from mailflow.extract.streaming import AttachmentTooLargeError

    ext = MimeExtractor(allowlist=frozenset({"text/plain"}), max_attachment_bytes=4)
    with pytest.raises(AttachmentBlockedError):
        _extract(ext, _raw_with_pdf())
    # and definitively NOT the cap error:
    try:
        _extract(ext, _raw_with_pdf())
    except AttachmentTooLargeError:
        raise AssertionError("cap ran before allowlist — reorder regressed")
    except AttachmentBlockedError:
        pass
