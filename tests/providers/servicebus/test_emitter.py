import hashlib
import json

from mailflow.adapters.servicebus.emitter import ServiceBusEmitter
from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, body: bytes, message_id: str, content_type: str, session_id):
        self.sent.append(
            {"body": body, "message_id": message_id, "content_type": content_type, "session_id": session_id}
        )


def _event() -> EmailEvent:
    email = CleanEmail(
        canonical_id="<m1@x>",
        provider="memory",
        provider_message_id="MSG123",
        provider_stream_id="s",
        subject="hi",
        schema_version=SCHEMA_VERSION,
    )
    return EmailEvent(
        tenant="acme",
        ordering_key="ops@acme.com",
        idempotency_key="acme|ops@acme.com|MSG123",
        email=email,
    )


def test_emit_sends_json_body_keyed_by_idempotency_key():
    sender = _FakeSender()
    receipt = ServiceBusEmitter(sender=sender).emit(_event())

    assert len(sender.sent) == 1
    msg = sender.sent[0]
    # SB duplicate-detection key == idempotency_key (transport-level dedupe backstop)
    assert msg["message_id"] == "acme|ops@acme.com|MSG123"
    assert msg["content_type"] == "application/json"
    # ordering_key -> session_id so FIFO-per-mailbox is available if the queue uses sessions
    assert msg["session_id"] == "ops@acme.com"
    payload = json.loads(msg["body"].decode("utf-8"))
    assert payload["tenant"] == "acme"
    assert payload["email"]["subject"] == "hi"
    # Emitter returns a receipt (Emitter.emit -> object)
    assert getattr(receipt, "accepted", False) is True


def test_long_idempotency_key_is_hashed_to_fit_sb_message_id_cap():
    long_key = "acme|ops@acme.com|" + ("M" * 200)  # far past SB's 128-char MessageId cap
    email = CleanEmail(
        canonical_id="<m1@x>",
        provider="memory",
        provider_message_id="MSG123",
        provider_stream_id="s",
        subject="hi",
        schema_version=SCHEMA_VERSION,
    )
    event = EmailEvent(
        tenant="acme",
        ordering_key="ops@acme.com",
        idempotency_key=long_key,
        email=email,
    )
    sender = _FakeSender()
    ServiceBusEmitter(sender=sender).emit(event)

    msg = sender.sent[0]
    expected = hashlib.sha256(long_key.encode("utf-8")).hexdigest()
    assert msg["message_id"] == expected
    assert len(msg["message_id"]) <= 128

    # Same idempotency_key -> same message_id (SB duplicate-detection still works).
    sender2 = _FakeSender()
    ServiceBusEmitter(sender=sender2).emit(event)
    assert sender2.sent[0]["message_id"] == expected
