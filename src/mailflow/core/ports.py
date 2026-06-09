"""The ports — structural contracts the core depends on. NO vendor imports.

Per spec §5.3 / R-D5, @runtime_checkable only checks method-name existence and is
slow; static typing (mypy strict) is the real conformance gate.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Protocol, runtime_checkable

from mailflow.core.events import EmailEvent
from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import (
    CleanEmail,
    Cursor,
    Envelope,
    RawMessage,
    Relevance,
    StreamRef,
)


@runtime_checkable
class MailboxProvider(Protocol):
    def connect(self) -> None: ...
    def sync_streams(self) -> Iterable[StreamRef]: ...
    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]: ...
    def message_size(self, msg: RawMessage) -> int | None: ...


@runtime_checkable
class SubscriptionManager(Protocol):
    def ensure_watch(self, stream: StreamRef) -> object: ...
    def renew_watch(self, handle: object) -> object: ...


@runtime_checkable
class EnvelopeParser(Protocol):
    def parse_envelope(self, msg: RawMessage, tenant: str) -> Envelope: ...


@runtime_checkable
class Filter(Protocol):
    name: str
    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision: ...


@runtime_checkable
class Classifier(Protocol):
    def classify(self, env: Envelope, ctx: FilterContext) -> Relevance: ...


@runtime_checkable
class ContentExtractor(Protocol):
    def extract(self, msg: RawMessage, env: Envelope) -> CleanEmail: ...


@runtime_checkable
class Emitter(Protocol):
    def emit(self, event: EmailEvent) -> object: ...


@runtime_checkable
class CursorStore(Protocol):
    def get(self, tenant: str, stream: StreamRef) -> Cursor | None: ...
    def commit_if_ahead(self, tenant: str, stream: StreamRef, cursor: Cursor) -> bool: ...


@runtime_checkable
class DedupeStore(Protocol):
    def try_claim(self, key: str, lease_seconds: int) -> bool: ...
    def record_attempt(self, key: str) -> int: ...
    def mark_done(self, key: str, ttl_seconds: int) -> None: ...
    def release(self, key: str) -> None: ...


@runtime_checkable
class SecretProvider(Protocol):
    def get(self, ref: str) -> str: ...


@runtime_checkable
class BlobStore(Protocol):
    def put_stream(self, ref: str, chunks: Iterator[bytes], content_type: str) -> str: ...
    def open(self, ref: str) -> Iterator[bytes]: ...
