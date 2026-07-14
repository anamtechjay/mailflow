"""Pipeline builder + fault injection + the Graph fake HTTP transport.

`build_memory_pipeline` is the one true way to wire a memory `Pipeline` for QA
tests (mirrors `tests/core/test_pipeline_observers.py::_pipeline`, generalized).
`FaultExtractor` raises `TransientError` for its first `fail_until` calls, then
delegates to a real `MimeExtractor` — for exercising the bounded-retry path
(spec §A2) without a real flaky network. The `_FakeGraphTransport` family is
copied from `tests/providers/servicebus/test_e2e.py` so later provider tests
can reuse it without importing across test modules.
"""

from __future__ import annotations

from typing import Any, Literal

from mailflow.core.errors import TransientError
from mailflow.core.models import CleanEmail, Envelope, RawMessage, StreamRef
from mailflow.core.observability import Observers, TraceObserver
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.ports import ContentExtractor, Emitter, Filter
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule, normalize_attachment_policy
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

DEFAULT_TENANT = "acme"


def build_memory_pipeline(
    *,
    seed: dict[StreamRef, list[SeedEmail]],
    emitter: Emitter | None = None,
    filters: list[Filter] | None = None,
    on_filtered: Literal["tag", "drop"] = "tag",
    max_message_bytes: int = 50_000_000,
    attachments: AttachmentPolicy | AttachmentRule | dict[str, Any] | None = None,
    extractor: ContentExtractor | MimeExtractor | None = None,
    observers_on_trace: TraceObserver | None = None,
    stores: dict[str, Any] | None = None,
) -> Pipeline:
    """Build a real memory-backed `Pipeline` with sane defaults for QA tests.

    - `emitter` defaults to a fresh `MemoryEmitter`.
    - `stores` (a `dict` with `cursor_store`/`dedupe_store`/`blob_store`, e.g. the
      `mem_stores`/`sqlite_stores` fixtures) defaults to fresh in-memory stores.
    - `attachments` is normalized via `normalize_attachment_policy` and passed to a
      fresh `MimeExtractor`, unless `extractor` is given (in which case
      `attachments` must be None — they are mutually exclusive knobs).
    - `observers_on_trace` wraps a single `on_trace` callback in `Observers`.
    """
    if extractor is not None and attachments is not None:
        raise ValueError("pass either `extractor` or `attachments`, not both")

    stores = stores or {}
    cursor_store = stores.get("cursor_store") or InMemoryCursorStore()
    dedupe_store = stores.get("dedupe_store") or InMemoryDedupeStore()
    blob_store = stores.get("blob_store") or InMemoryBlobStore()

    resolved_extractor: ContentExtractor | MimeExtractor
    if extractor is not None:
        resolved_extractor = extractor
    else:
        resolved_extractor = MimeExtractor(attachment_policy=normalize_attachment_policy(attachments))

    observers = Observers(on_trace=observers_on_trace) if observers_on_trace is not None else Observers()

    return Pipeline(
        provider=MemoryProvider(seed=seed),
        parser=MimeEnvelopeParser(),
        filters=FilterChain(filters or []),
        extractor=resolved_extractor,
        emitter=emitter or MemoryEmitter(),
        dlq_emitter=MemoryEmitter(),
        cursor_store=cursor_store,
        dedupe_store=dedupe_store,
        blob_store=blob_store,
        config=PipelineConfig(
            tenant=DEFAULT_TENANT, on_filtered=on_filtered, max_message_bytes=max_message_bytes,
        ),
        observers=observers,
    )


class FaultExtractor:
    """`ContentExtractor` that raises `TransientError` for its first `fail_until`
    calls (across the FaultExtractor's lifetime, not per-message), then delegates
    to a real `MimeExtractor`. Lets a test assert the pipeline's bounded-retry /
    DLQ behavior (spec §A2) deterministically, without a flaky real dependency.
    """

    def __init__(self, fail_until: int, *, inner: MimeExtractor | None = None) -> None:
        self.fail_until = fail_until
        self.calls = 0
        self._inner = inner or MimeExtractor()

    def extract(self, msg: RawMessage, env: Envelope) -> CleanEmail:
        self.calls += 1
        if self.calls <= self.fail_until:
            raise TransientError(f"FaultExtractor: synthetic failure {self.calls}/{self.fail_until}")
        return self._inner.extract_bytes(
            msg.raw_bytes,
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            stream_id=msg.stream.key,
            watched_mailbox=msg.stream.mailbox,
            thread_key=msg.thread_key,
        )


# =========================================================================== Graph fakes
# Copied from tests/providers/servicebus/test_e2e.py so provider-focused QA tests
# (Phase 3) can build a fake Graph transport without importing across test modules.


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        self.content = b""

    def json(self) -> Any:
        return self._payload


class _FakeGraphTransport:
    """Routes Graph REST GETs to canned JSON, keyed by the message id in the URL.

    `messages`: message_id -> message JSON (returned by GET .../messages/{id}).
    `attachments`: message_id -> attachment metadata list.
    `errors`: message_id -> HTTP status to return instead (e.g. 404 deleted, 500 infra).
    """

    def __init__(
        self,
        messages: dict[str, dict[str, Any]],
        attachments: dict[str, list[dict[str, Any]]] | None = None,
        errors: dict[str, int] | None = None,
    ) -> None:
        self.messages = messages
        self.attachments = attachments or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, *, headers: dict[str, str], json: Any | None) -> _Resp:
        self.calls.append((method, url))
        if "/attachments" in url:
            mid = url.split("/messages/")[1].split("/attachments")[0]
            return _Resp(200, {"value": self.attachments.get(mid, [])})
        if "/messages/" in url:  # single message GET: .../messages/{id}?$select=...
            mid = url.split("/messages/")[1].split("?")[0]
            if mid in self.errors:
                return _Resp(self.errors[mid], {"error": {"message": "boom"}})
            if mid in self.messages:
                return _Resp(200, self.messages[mid])
            return _Resp(404, {"error": {"message": "not found"}})
        # internetMessageId $filter fallback query (.../messages?$filter=...): no match
        return _Resp(200, {"value": []})


def _graph_message(
    msg_id: str = "MSG1",
    *,
    mailbox: str = "ops@acme.com",
    sender: str = "alice@partner.com",
    subject: str = "Invoice #42",
    body: str = "hello body",
    content_type: str = "text",
    has_attachments: bool = False,
    conversation_id: str = "",
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "internetMessageId": f"<{msg_id}@partner.com>",
        "from": {"emailAddress": {"name": "Alice", "address": sender}},
        "toRecipients": [{"emailAddress": {"address": mailbox}}],
        "ccRecipients": [],
        "subject": subject,
        "body": {"contentType": content_type, "content": body},
        "bodyPreview": body[:50],
        "receivedDateTime": "2026-07-03T10:00:00Z",
        "sentDateTime": "2026-07-03T09:59:00Z",
        "isDraft": False,
        "hasAttachments": has_attachments,
        "parentFolderId": "inbox",
        "categories": [],
        "conversationId": conversation_id,
    }


__all__ = [
    "build_memory_pipeline",
    "FaultExtractor",
    "_Resp",
    "_FakeGraphTransport",
    "_graph_message",
]
