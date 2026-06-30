"""Core value models. Pure data, no behaviour beyond derivation helpers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from functools import total_ordering

from pydantic import BaseModel, ConfigDict, Field


class Direction(str, Enum):
    inbound = "inbound"
    outbound = "outbound"
    unknown = "unknown"


class Disposition(str, Enum):
    """The four terminal states a message can reach (spec §8.1)."""

    emitted = "emitted"
    dropped = "dropped"
    duplicate = "duplicate"
    dead_lettered = "dead_lettered"


class Decision(str, Enum):
    """Three-valued filter outcome (spec §7.4)."""

    keep = "keep"
    drop = "drop"
    uncertain = "uncertain"


class Verdict(str, Enum):
    relevant = "relevant"
    not_relevant = "not_relevant"
    unknown = "unknown"


class ScanVerdict(str, Enum):
    """Outcome of an attachment safety scan (V1 fast-follow)."""

    allow = "allow"
    block = "block"


class StripReason(str, Enum):
    """Why an attachment was stripped rather than delivered or rejected."""

    not_allowlisted = "not_allowlisted"
    oversize = "oversize"
    unreadable = "unreadable"
    scanner = "scanner"


class ScanResult(BaseModel):
    """An AttachmentScanner / allowlist verdict. Allow by default (no-op posture)."""

    verdict: ScanVerdict = ScanVerdict.allow
    reason: str = ""


class StrippedAttachment(BaseModel):
    """Record of an attachment that was stripped before delivery (schema 1.3)."""

    filename: str = ""
    content_type: str = ""
    size_bytes: int = 0
    is_inline: bool = False
    reason: StripReason


class Recipient(BaseModel):
    name: str = ""
    address: str = ""


class StreamRef(BaseModel):
    """One sync stream: a mailbox (Gmail) or a mailbox folder (Graph) — spec §8.3."""

    model_config = ConfigDict(frozen=True)

    mailbox: str
    folder: str | None = None

    @property
    def key(self) -> str:
        return f"{self.mailbox}:{self.folder}" if self.folder else self.mailbox


@total_ordering
class Cursor(BaseModel):
    """Opaque per-stream bookmark. `order` is the monotonic comparator (spec §8.3)."""

    model_config = ConfigDict(frozen=True)

    value: str
    order: int

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Cursor):
            return NotImplemented
        return self.order < other.order

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Cursor):
            return NotImplemented
        return self.order == other.order

    def __hash__(self) -> int:
        return hash((self.value, self.order))


class RawMessage(BaseModel):
    """A fetched message: cheap metadata always present; `raw_bytes` is the heavy
    payload only touched during EXTRACT (spec §7.3 vs §7.6)."""

    provider: str
    provider_message_id: str
    stream: StreamRef
    size_bytes: int
    received_at: datetime
    cursor: Cursor
    raw_bytes: bytes = b""
    thread_key: str = ""  # provider thread/conversation id, populated by the adapter (A7)


class Attachment(BaseModel):
    filename: str = ""
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    content_hash: str = ""  # sha256 of bytes (spec §6.2)
    content_id: str = ""
    is_inline: bool = False
    provider_attachment_id: str = ""
    storage_ref: str = ""  # pointer in BlobStore, NOT the bytes

    model_config = ConfigDict(populate_by_name=True)


class Relevance(BaseModel):
    """Classifier output — non-destructive by default (spec OD-2)."""

    verdict: Verdict = Verdict.unknown
    score: float | None = None
    reason: str = ""


class Envelope(BaseModel):
    """Cheap, provider-neutral parse used by filters BEFORE full extraction (§7.3)."""

    canonical_id: str
    message_id: str | None = None
    message_id_present: bool = False
    message_id_trusted: bool = False
    provider: str
    provider_message_id: str
    stream: StreamRef
    from_: Recipient = Field(default_factory=Recipient, alias="from")
    sender: Recipient | None = None
    reply_to: Recipient | None = None
    to: list[Recipient] = Field(default_factory=list)
    cc: list[Recipient] = Field(default_factory=list)
    subject: str = ""
    date_utc: datetime | None = None
    received_at: datetime | None = None
    snippet: str = ""
    list_id: str | None = None
    list_unsubscribe: str | None = None
    auto_submitted: str | None = None
    is_auto_submitted: bool = False
    is_bounce: bool = False
    headers: dict[str, list[str]] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class CleanEmail(BaseModel):
    """The normalized, provider-agnostic email we emit (spec §6.2)."""

    canonical_id: str
    message_id: str | None = None
    message_id_present: bool = False
    message_id_trusted: bool = False
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)

    provider: str
    provider_message_id: str
    provider_stream_id: str
    direction: Direction = Direction.unknown
    is_draft: bool = False

    from_: Recipient = Field(default_factory=Recipient, alias="from")
    sender: Recipient | None = None
    reply_to: Recipient | None = None
    to: list[Recipient] = Field(default_factory=list)
    cc: list[Recipient] = Field(default_factory=list)
    bcc: list[Recipient] = Field(default_factory=list)

    subject: str = ""
    date_utc: datetime | None = None
    received_at: datetime | None = None

    body_text: str = ""
    body_html: str = ""
    body_truncated: bool = False
    attachments: list[Attachment] = Field(default_factory=list)
    stripped_attachments: list[StrippedAttachment] = Field(default_factory=list)

    labels: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    folder: str = ""
    thread_key: str = ""  # Gmail threadId / Graph conversationId; subject-fallback in Phase 1 (A7)

    auto_submitted: str | None = None
    is_auto_submitted: bool = False
    is_bounce: bool = False
    list_id: str | None = None
    list_unsubscribe: str | None = None
    message_size_bytes: int = 0
    raw_headers: dict[str, list[str]] = Field(default_factory=dict)

    relevance: Relevance = Field(default_factory=Relevance)
    matched_filter: str = ""
    schema_version: str = ""

    model_config = ConfigDict(populate_by_name=True)


class WebhookIdentity(BaseModel):
    """Identity-only result of a verified push notification (A5).

    Carries *who* the verified ping is for — never any trusted payload. The
    consumer still re-derives content from the cursor (wake-signal-only rule).
    """

    provider: str
    mailbox: str | None = None
    stream_id: str | None = None
    subscription_id: str | None = None

    model_config = ConfigDict(frozen=True)
