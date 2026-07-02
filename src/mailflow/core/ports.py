"""The ports — structural contracts the core depends on. NO vendor imports.

Per spec §5.3 / R-D5, @stable
@runtime_checkable only checks method-name existence and is
slow; static typing (mypy strict) is the real conformance gate.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Mapping, Protocol, TypeVar, runtime_checkable

from mailflow.core.events import EmailEvent
from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import (
    Attachment,
    CleanEmail,
    Cursor,
    Envelope,
    RawMessage,
    Relevance,
    ScanResult,
    StreamRef,
    WebhookIdentity,
)
from mailflow.core.observability import DeadLetterRecord

# V1 runs synchronously — one message at a time, no internal concurrency. A future
# async family is an *additive* set of ports, never a breaking change to these (A11).
SYNC_ONLY = True

_T = TypeVar("_T")


def stable(cls: _T) -> _T:
    """Mark a port as part of the frozen V1 surface (A11). Not a Protocol member."""
    setattr(cls, "__stability__", "stable")
    return cls


def provisional(cls: _T) -> _T:
    """Mark a port as provisional — shipped in V1 but may still evolve (A11)."""
    setattr(cls, "__stability__", "provisional")
    return cls


@stable
@runtime_checkable
class MailboxProvider(Protocol):
    def connect(self) -> None: ...
    def sync_streams(self) -> Iterable[StreamRef]: ...
    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]: ...
    def message_size(self, msg: RawMessage) -> int | None: ...


@stable
@runtime_checkable
class AuthRefresher(Protocol):
    """Forces a credential refresh after a 401 so the in-process AuthError retry
    re-authenticates with a fresh token (A2)."""

    def force_refresh(self) -> None: ...


@stable
@runtime_checkable
class SubscriptionManager(Protocol):
    def ensure_watch(self, stream: StreamRef) -> object: ...
    def renew_watch(self, handle: object) -> object: ...


@stable
@runtime_checkable
class EnvelopeParser(Protocol):
    def parse_envelope(self, msg: RawMessage, tenant: str) -> Envelope: ...


@stable
@runtime_checkable
class Filter(Protocol):
    name: str
    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision: ...


@stable
@runtime_checkable
class Classifier(Protocol):
    def classify(self, env: Envelope, ctx: FilterContext) -> Relevance: ...


@stable
@runtime_checkable
class ContentExtractor(Protocol):
    def extract(self, msg: RawMessage, env: Envelope) -> CleanEmail: ...


@stable
@runtime_checkable
class Emitter(Protocol):
    def emit(self, event: EmailEvent) -> object: ...


@stable
@runtime_checkable
class CursorStore(Protocol):
    def get(self, tenant: str, stream: StreamRef) -> Cursor | None: ...
    def commit_if_ahead(self, tenant: str, stream: StreamRef, cursor: Cursor) -> bool: ...


@stable
@runtime_checkable
class DedupeStore(Protocol):
    def try_claim(self, key: str, lease_seconds: int) -> bool: ...
    def record_attempt(self, key: str) -> int: ...
    def mark_done(self, key: str, ttl_seconds: int) -> None: ...
    def release(self, key: str) -> None: ...


@stable
@runtime_checkable
class SecretProvider(Protocol):
    def get(self, ref: str) -> str: ...


@stable
@runtime_checkable
class BlobStore(Protocol):
    def put_stream(self, ref: str, chunks: Iterator[bytes], content_type: str) -> str: ...
    def open(self, ref: str) -> Iterator[bytes]: ...


@stable
@runtime_checkable
class DeadLetterStore(Protocol):
    """Durable, replayable dead-letter records (A2 redrive). Separate from the
    fire-and-forget `dlq_emitter` sink: this one is queryable + deletable so an operator
    can redrive after fixing the root cause."""

    def put(self, record: DeadLetterRecord) -> None: ...
    def list_pending(self, *, limit: int | None = None) -> list[DeadLetterRecord]: ...
    def delete(self, record_id: str) -> None: ...


@provisional
@runtime_checkable
class WebhookVerifier(Protocol):
    """Proves a push notification is genuine; returns identity only, no payload trust (A5)."""

    def verify(self, *, headers: Mapping[str, str], body: bytes) -> WebhookIdentity: ...


@provisional
@runtime_checkable
class ContentCleaner(Protocol):
    """Swappable HTML->text cleaning stage; gentle by default (A6)."""

    def clean(self, email: CleanEmail) -> CleanEmail: ...


@provisional
@runtime_checkable
class TokenRotationSink(Protocol):
    """Persists a rotated OAuth refresh token so the next run survives (A8)."""

    def on_refresh(self, ref: str, new_token: str) -> None: ...


@provisional
@runtime_checkable
class AttachmentScanner(Protocol):
    """Safety hook run on each attachment BEFORE its bytes are persisted. The V1
    default is a no-op (allow-all); a real AV/CDR scanner is a P2 concern.

    NOTE — V1 seam limitation: `scan` receives attachment METADATA only
    (filename / content_type / size_bytes / content_hash), NOT the raw bytes. A
    real AV/CDR scanner that needs the content is a P2 concern that may evolve
    this provisional signature (e.g. to accept a bytes-stream accessor)."""

    def scan(self, attachment: Attachment) -> ScanResult: ...
