"""Task 4: connect() `attachments=` wiring + public-export tests.

Verifies that the `attachments` parameter on `connect()` is threaded through
to the MimeExtractor so offending parts are stripped (not DLQ'd) and the
emitted CleanEmail carries stripped_attachments records.

Also verifies the four new public names are importable from the top-level
`mailflow` package.
"""

from __future__ import annotations

import base64

from mailflow import connect
from mailflow.core.models import StreamRef, StripReason
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule
from mailflow.providers.memory import SeedEmail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n png pixel bytes"  # 20-ish bytes, an "inline" image


def _b64(data: bytes) -> bytes:
    return base64.b64encode(data)


def _multipart_with_inline(inline_data: bytes = PNG) -> bytes:
    """Build a minimal RFC822 multipart/mixed with text body + inline image."""
    return (
        b"Message-ID: <attach-test@x>\r\n"
        b"From: a@partner.com\r\n"
        b"To: ops@acme.com\r\n"
        b"Subject: strip me\r\n"
        b'Content-Type: multipart/mixed; boundary="BB"\r\n'
        b"\r\n"
        b"--BB\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"hello body\r\n"
        b"--BB\r\n"
        b'Content-Type: image/png; name="logo.png"\r\n'
        b"Content-ID: <logo1>\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n" + _b64(inline_data) + b"\r\n"
        b"--BB--\r\n"
    )


STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _seed_raw(raw: bytes) -> dict[StreamRef, list[SeedEmail]]:
    return {STREAM: [SeedEmail("m1", raw)]}


# ---------------------------------------------------------------------------
# Main end-to-end strip test
# ---------------------------------------------------------------------------


def test_connect_attachments_policy_strips_oversize_inline() -> None:
    """AttachmentPolicy(inline=AttachmentRule(max_bytes=4)) strips the 20-byte
    inline image; the message is still emitted and stripped_attachments is populated."""
    raw = _multipart_with_inline()
    policy = AttachmentPolicy(inline=AttachmentRule(max_bytes=4))
    mf = connect("memory", seed=_seed_raw(raw), attachments=policy)
    emails = mf.fetch_new()

    assert len(emails) == 1, "message must still be emitted (not DLQ'd)"
    email = emails[0]
    assert email.attachments == [], "stripped part must not appear in attachments"
    assert len(email.stripped_attachments) == 1
    sa = email.stripped_attachments[0]
    assert sa.reason is StripReason.oversize
    assert sa.is_inline is True
    assert sa.content_type == "image/png"


# ---------------------------------------------------------------------------
# Normalization paths through connect()
# ---------------------------------------------------------------------------


def test_connect_attachments_bare_rule_broadcasts_to_both_classes() -> None:
    """connect(..., attachments=AttachmentRule(max_bytes=4)) — bare rule applied to
    both inline and real; the inline image is still stripped."""
    raw = _multipart_with_inline()
    rule = AttachmentRule(max_bytes=4)
    mf = connect("memory", seed=_seed_raw(raw), attachments=rule)
    emails = mf.fetch_new()

    assert len(emails) == 1
    assert len(emails[0].stripped_attachments) == 1
    assert emails[0].stripped_attachments[0].reason is StripReason.oversize


def test_connect_attachments_bare_dict_broadcasts() -> None:
    """connect(..., attachments={"max_bytes": 4}) — bare dict normalised to rule
    applied to both classes; inline image is stripped."""
    raw = _multipart_with_inline()
    mf = connect("memory", seed=_seed_raw(raw), attachments={"max_bytes": 4})
    emails = mf.fetch_new()

    assert len(emails) == 1
    assert len(emails[0].stripped_attachments) == 1


def test_connect_attachments_per_class_dict() -> None:
    """connect(..., attachments={"inline": {"max_bytes": 4}}) — per-class dict, only
    inline rule is tightened; inline image stripped, real class keeps defaults."""
    raw = _multipart_with_inline()
    mf = connect("memory", seed=_seed_raw(raw), attachments={"inline": {"max_bytes": 4}})
    emails = mf.fetch_new()

    assert len(emails) == 1
    assert len(emails[0].stripped_attachments) == 1
    assert emails[0].stripped_attachments[0].is_inline is True


def test_connect_attachments_none_default_delivers_without_strip() -> None:
    """Default attachments=None must deliver the inline image unchanged (no strip)."""
    raw = _multipart_with_inline()
    mf = connect("memory", seed=_seed_raw(raw))
    emails = mf.fetch_new()

    assert len(emails) == 1
    # Without a policy the inline image is kept (no oversize or allowlist rule)
    assert emails[0].stripped_attachments == []


# ---------------------------------------------------------------------------
# Public-export test
# ---------------------------------------------------------------------------


def test_public_exports_importable() -> None:
    """All four new names must be importable from the top-level `mailflow` package."""
    from mailflow import AttachmentPolicy as AP  # noqa: F401
    from mailflow import AttachmentRule as AR  # noqa: F401
    from mailflow import StrippedAttachment as SA  # noqa: F401
    from mailflow import StripReason as SR  # noqa: F401

    # Smoke-check they are the real types
    assert AP is AttachmentPolicy
    assert AR is AttachmentRule
