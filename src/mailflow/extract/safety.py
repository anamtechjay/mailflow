"""Attachment safety seam (V1 fast-follow): a swappable scanner port default + a
content-type/extension allowlist, run on each attachment BEFORE its bytes are
persisted. V1 ships the SLOT with a no-op scanner and an empty (allow-all)
allowlist; a real AV/CDR scanner is a P2 concern. A blocked attachment fails
closed: the message is dead-lettered (PermanentError -> DLQ)."""

from __future__ import annotations

from mailflow.core.errors import PermanentError
from mailflow.core.models import Attachment, ScanResult, ScanVerdict

# Empty allowlist == no restriction (V1 default leaves current behaviour unchanged).
DEFAULT_ALLOWLIST: frozenset[str] = frozenset()


class AttachmentBlockedError(PermanentError):
    """An attachment failed the allowlist/scan and is quarantined (fail-closed -> DLQ)."""

    def __init__(self, filename: str, reason: str) -> None:
        self.filename = filename
        super().__init__(f"attachment blocked ({filename}): {reason}")


class NoOpAttachmentScanner:
    """V1 default AttachmentScanner: allows everything. The real scanner is P2."""

    def scan(self, attachment: Attachment) -> ScanResult:
        return ScanResult()  # verdict defaults to allow


def check_allowlist(attachment: Attachment, allowlist: frozenset[str]) -> ScanResult:
    """Allow when the attachment's content-type OR lowercased file extension is in
    `allowlist`. An empty allowlist allows everything (no restriction)."""
    if not allowlist:
        return ScanResult()
    name = attachment.filename
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if attachment.content_type.lower() in allowlist or ext in allowlist:
        return ScanResult()
    return ScanResult(
        verdict=ScanVerdict.block,
        reason=f"type not allowlisted: {attachment.content_type} / .{ext}",
    )
