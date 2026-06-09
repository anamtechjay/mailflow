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
