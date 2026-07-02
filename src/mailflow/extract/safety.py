"""Attachment safety seam (V1 fast-follow): a swappable scanner port default + a
content-type/extension allowlist, run on each attachment BEFORE its bytes are
persisted. V1 ships the SLOT with a no-op scanner and an empty (allow-all)
allowlist; a real AV/CDR scanner is a P2 concern. A blocked attachment fails
closed: the message is dead-lettered (PermanentError -> DLQ).

Scope + footgun (read before enabling an allowlist):
- The allowlist/scanner apply to BOTH real attachments AND inline media (logos,
  tracking pixels, CID-referenced images) — the MIME walker runs this check on
  every non-body part, not just disposition=attachment parts.
- A SINGLE blocked part fails the ENTIRE message: it raises and the whole message
  is quarantined (fail-closed -> DLQ). One blocked part dead-letters otherwise
  normal mail; it is not dropped per-part.
- The V1 default is an empty allowlist + no-op scanner == allow-all, so default
  behaviour is UNAFFECTED (nothing is ever blocked).
- Operators who enable a non-empty allowlist MUST include the inline media types
  they expect in normal mail (e.g. `image/png`, `image/jpeg` for logos); an
  allowlist scoped only to attachment types (e.g. `{"application/pdf"}`) will
  block an inline `image/png` logo and dead-letter ordinary messages."""

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
