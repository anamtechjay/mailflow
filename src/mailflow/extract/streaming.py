"""Chunked, fail-closed attachment streaming (V1 fast-follow).

The legacy path decoded a whole attachment into memory then wrapped it in
`iter([payload])` — no cap, and the decoded copy held whole in memory. This
module streams the decode of a leaf MIME part in chunks (avoiding a second full
copy of the decoded bytes) and enforces a hard per-attachment byte cap that fails
CLOSED: an over-cap or undecodable attachment raises a PermanentError, so the
pipeline dead-letters the message (parallels the B1 message-level size guard)
rather than persisting an over-cap blob.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import quopri
from email.message import EmailMessage
from typing import Iterator

from mailflow.core.errors import PermanentError

# Tighter than B1's 50 MB *message* ceiling (PipelineConfig.max_message_bytes).
MAX_ATTACHMENT_BYTES = 25_000_000  # 25 MB per attachment


class AttachmentTooLargeError(PermanentError):
    """A single attachment exceeded the per-attachment byte cap (fail-closed -> DLQ)."""

    def __init__(self, cap: int, seen: int) -> None:
        self.cap = cap
        self.seen = seen
        super().__init__(f"attachment exceeds the {cap}-byte cap (saw {seen} bytes)")


class AttachmentUnreadableError(PermanentError):
    """An attachment's bytes could not be decoded (unknown size -> fail-closed -> DLQ)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"attachment unreadable (fail-closed): {reason}")


def iter_decoded(part: EmailMessage, chunk_size: int = 65536) -> Iterator[bytes]:
    """Yield a leaf part's *decoded* bytes in chunks of ~`chunk_size`, without
    materialising the whole decoded payload at once. Supports base64 /
    quoted-printable / 7bit / 8bit / binary transfer encodings."""
    cte = (part.get("content-transfer-encoding") or "7bit").strip().lower()
    raw = part.get_payload(decode=False)
    if not isinstance(raw, str):
        raise AttachmentUnreadableError(f"payload is {type(raw).__name__}, not text")
    if cte == "base64":
        data = "".join(raw.split())  # drop the 76-col line folding / whitespace
        step = (chunk_size // 3 + 1) * 4  # decode on 4-char -> 3-byte quanta boundaries
        for i in range(0, len(data), step):
            try:
                yield base64.b64decode(data[i : i + step])
            except binascii.Error as exc:
                raise AttachmentUnreadableError(f"invalid base64: {exc}") from exc
    elif cte == "quoted-printable":
        # Decoded QP is never larger than its encoded form -> fail closed pre-decode.
        encoded = raw.encode("latin-1", "surrogateescape")
        yield quopri.decodestring(encoded)
    else:  # 7bit / 8bit / binary / identity — get true bytes, not the policy-mangled str
        decoded = part.get_payload(decode=True)
        if not isinstance(decoded, bytes):
            raise AttachmentUnreadableError(
                f"payload is {type(decoded).__name__}, not bytes"
            )
        for i in range(0, len(decoded), chunk_size):
            yield decoded[i : i + chunk_size]


def digest_and_size(part: EmailMessage, *, cap: int, chunk_size: int = 65536) -> tuple[str, int]:
    """Stream the part once to compute (sha256_hex, size_bytes), enforcing the cap
    fail-closed. Nothing is buffered beyond a single chunk; raises before the running
    count can exceed `cap`. Returns ("", 0) for a genuinely empty part (preserving the
    legacy `content_hash = "" if not payload` behaviour)."""
    hasher = hashlib.sha256()
    total = 0
    for chunk in iter_decoded(part, chunk_size):
        total += len(chunk)
        if total > cap:
            raise AttachmentTooLargeError(cap, total)
        hasher.update(chunk)
    return (hasher.hexdigest(), total) if total else ("", 0)
