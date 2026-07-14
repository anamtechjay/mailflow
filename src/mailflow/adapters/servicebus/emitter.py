"""ServiceBusEmitter — egress sink implementing the core Emitter port. Serializes each
EmailEvent to JSON and sends it to a Service Bus queue/topic. The transport SDK is NOT
imported here: a duck-typed sender is injected (adapted from azure-servicebus in live.py),
so this stays unit-testable with a fake. The message_id is the event's idempotency_key
(so Service Bus duplicate-detection is a transport-level backstop to the pipeline dedupe),
and ordering_key maps to session_id for optional per-mailbox FIFO."""

from __future__ import annotations

import hashlib
from typing import Protocol

from pydantic import BaseModel

from mailflow.core.events import EmailEvent

# Service Bus caps ServiceBusMessage.message_id at 128 characters; sends past that raise.
# idempotency_key = "tenant|mailbox|provider_message_id" can exceed it (long Graph ids).
_SB_MESSAGE_ID_MAX = 128


class SbSender(Protocol):
    def send(
        self, *, body: bytes, message_id: str, content_type: str, session_id: str | None
    ) -> None: ...


class EmitReceipt(BaseModel):
    id: str
    accepted: bool = True


def _sb_message_id(key: str) -> str:
    """Map an idempotency key to a Service Bus MessageId. Keys <= 128 chars pass through
    unchanged; longer keys are replaced with their sha256 hex digest (64 chars) so the
    SAME idempotency_key always maps to the SAME message_id, preserving SB's
    duplicate-detection semantics even when the source key is oversized."""
    if len(key) <= _SB_MESSAGE_ID_MAX:
        return key
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class ServiceBusEmitter:
    def __init__(self, *, sender: SbSender) -> None:
        self._sender = sender

    def emit(self, event: EmailEvent) -> EmitReceipt:
        body = event.model_dump_json().encode("utf-8")
        message_id = _sb_message_id(event.idempotency_key or event.email.canonical_id)
        session_id = event.ordering_key or None
        self._sender.send(
            body=body,
            message_id=message_id,
            content_type="application/json",
            session_id=session_id,
        )
        return EmitReceipt(id=event.email.canonical_id, accepted=True)
