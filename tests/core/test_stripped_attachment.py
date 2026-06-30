"""Task 1 — StripReason, StrippedAttachment, CleanEmail.stripped_attachments (schema 1.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mailflow.core.models import CleanEmail, StripReason, StrippedAttachment


def _email() -> CleanEmail:
    return CleanEmail(
        canonical_id="c1",
        provider="gmail",
        provider_message_id="m1",
        provider_stream_id="s1",
    )


# ---------------------------------------------------------------------------
# StripReason enum
# ---------------------------------------------------------------------------


def test_strip_reason_members_exist() -> None:
    assert StripReason.not_allowlisted.value == "not_allowlisted"
    assert StripReason.oversize.value == "oversize"
    assert StripReason.unreadable.value == "unreadable"
    assert StripReason.scanner.value == "scanner"


def test_strip_reason_is_str_enum() -> None:
    assert StripReason.not_allowlisted == "not_allowlisted"


# ---------------------------------------------------------------------------
# StrippedAttachment model
# ---------------------------------------------------------------------------


def test_stripped_attachment_requires_reason() -> None:
    with pytest.raises(ValidationError):
        StrippedAttachment()  # type: ignore[call-arg]


def test_stripped_attachment_defaults() -> None:
    sa = StrippedAttachment(reason=StripReason.oversize)
    assert sa.filename == ""
    assert sa.content_type == ""
    assert sa.size_bytes == 0
    assert sa.is_inline is False
    assert sa.reason == StripReason.oversize


def test_stripped_attachment_all_fields() -> None:
    sa = StrippedAttachment(
        filename="virus.exe",
        content_type="application/octet-stream",
        size_bytes=12345,
        is_inline=True,
        reason=StripReason.scanner,
    )
    assert sa.filename == "virus.exe"
    assert sa.content_type == "application/octet-stream"
    assert sa.size_bytes == 12345
    assert sa.is_inline is True
    assert sa.reason == StripReason.scanner


# ---------------------------------------------------------------------------
# CleanEmail.stripped_attachments
# ---------------------------------------------------------------------------


def test_clean_email_stripped_attachments_defaults_empty() -> None:
    assert _email().stripped_attachments == []


def test_clean_email_stripped_attachments_round_trips() -> None:
    sa = StrippedAttachment(reason=StripReason.not_allowlisted, filename="bad.pdf")
    email = _email()
    email.stripped_attachments = [sa]
    assert len(email.stripped_attachments) == 1
    assert email.stripped_attachments[0].filename == "bad.pdf"
    assert email.stripped_attachments[0].reason == StripReason.not_allowlisted
