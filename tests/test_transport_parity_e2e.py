"""Both Microsoft transports, end to end, plus parity between them.

The library supports two Graph delivery transports over ONE shared pipeline:

  * Azure Event Hubs      -> GraphEventHubsRuntime.process_batch  (checkpoint-after-success)
  * Event Grid -> Service Bus -> ServiceBusRuntime.process_messages (complete-after-success)

Both feed the SAME GraphProvider -> GraphEnvelopeParser -> GraphExtractor -> Pipeline, so the
ONLY difference is the transport wrapper + notification format. These tests drive each transport
end to end through a REAL pipeline (fake Graph HTTP + in-memory stores) and assert that the SAME
inbound mail yields an IDENTICAL CleanEmail on both — proving the transport choice is invisible
downstream. No Azure SDK, no network.
"""

from __future__ import annotations

import json
from typing import Any

from mailflow.adapters.graph.composition import build_graph_runtime
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.servicebus.composition import build_servicebus_graph_runtime
from mailflow.core.models import StreamRef
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

TENANT = "acme"
MBX = "ops@acme.com"
STREAM = StreamRef(mailbox=MBX, folder="inbox")


# --------------------------------------------------------------------------- shared fakes


class _FakeToken:
    def get_token(self) -> str:
        return "tok"


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        self.content = b""

    def json(self) -> Any:
        return self._payload


class _FakeGraphTransport:
    """Canned Graph REST responses keyed by message id in the URL (shared by both lanes)."""

    def __init__(self, messages: dict[str, dict[str, Any]]) -> None:
        self.messages = messages
        self.calls: list[str] = []

    def request(self, method: str, url: str, *, headers: dict[str, str], json: Any | None) -> _Resp:
        self.calls.append(url)
        if "/attachments" in url:
            return _Resp(200, {"value": []})
        if "/messages/" in url:
            mid = url.split("/messages/")[1].split("?")[0]
            if mid in self.messages:
                return _Resp(200, self.messages[mid])
            return _Resp(404, {"error": {"message": "not found"}})
        return _Resp(200, {"value": []})


# Event Hubs side
class _EHEvent:
    def __init__(self, body: str) -> None:
        self._body = body

    def body_as_str(self) -> str:
        return self._body


class _Checkpointer:
    def __init__(self) -> None:
        self.updated: list[Any] = []

    def update(self, event: Any) -> None:
        self.updated.append(event)


# Service Bus side
class _SbMsg:
    def __init__(self, body: str) -> None:
        self._body = body

    def body_as_str(self) -> str:
        return self._body


class _Receiver:
    def __init__(self) -> None:
        self.completed: list[Any] = []
        self.abandoned: list[Any] = []

    def complete(self, message: Any) -> None:
        self.completed.append(message)

    def abandon(self, message: Any) -> None:
        self.abandoned.append(message)


# --------------------------------------------------------------------------- message + notifications


def _graph_message(msg_id: str = "MSG1", *, subject: str = "Invoice #42") -> dict[str, Any]:
    return {
        "id": msg_id,
        "internetMessageId": f"<{msg_id}@partner.com>",
        "from": {"emailAddress": {"name": "Alice", "address": "alice@partner.com"}},
        "toRecipients": [{"emailAddress": {"address": MBX}}],
        "ccRecipients": [],
        "subject": subject,
        "body": {"contentType": "text", "content": "hello body"},
        "bodyPreview": "hello body",
        "receivedDateTime": "2026-07-05T10:00:00Z",
        "sentDateTime": "2026-07-05T09:59:00Z",
        "isDraft": False,
        "hasAttachments": False,
        "parentFolderId": "inbox",
        "categories": [],
    }


def _eventhub_notification(msg_id: str = "MSG1", *, client_state: str = "mailflow") -> str:
    """Raw Graph changeNotificationCollection — the Event Hubs body format."""
    return json.dumps({
        "value": [{
            "subscriptionId": "sub-1",
            "changeType": "created",
            "clientState": client_state,
            "resource": f"Users/{MBX}/Messages/{msg_id}",
            "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": msg_id},
        }]
    })


def _servicebus_message(msg_id: str = "MSG1", *, client_state: str = "mailflow") -> str:
    """Event Grid CloudEvent wrapping the same notification — the Service Bus body format."""
    return json.dumps({
        "type": "Microsoft.Graph.MessageCreated",
        "subject": f"Users/{MBX}/Messages/{msg_id}",
        "data": {
            "subscriptionId": "sub-1",
            "changeType": "created",
            "clientState": client_state,
            "resource": f"Users/{MBX}/Messages/{msg_id}",
            "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": msg_id},
        },
    })


# --------------------------------------------------------------------------- runtime builders


def _eventhub_runtime(messages: dict[str, dict[str, Any]], emitter: Any):
    return build_graph_runtime(
        graph_cfg=GraphConfig(tenant_id="t", client_id="c", client_secret_ref="env://X", mailboxes=[MBX]),
        eventhub=EventHubConfig(namespace="evh", hub="graph-notifications", tenant_domain="acme.com"),
        tenant=TENANT,
        token_provider=_FakeToken(),
        transport=_FakeGraphTransport(messages),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
    )


def _servicebus_runtime(messages: dict[str, dict[str, Any]], emitter: Any):
    return build_servicebus_graph_runtime(
        graph_cfg=GraphConfig(tenant_id="t", client_id="c", client_secret_ref="env://X", mailboxes=[MBX]),
        tenant=TENANT,
        token_provider=_FakeToken(),
        transport=_FakeGraphTransport(messages),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
    )


# =========================================================================== Event Hubs lane


def test_eventhub_transport_emits_end_to_end_and_checkpoints():
    sink = MemoryEmitter()
    rt = _eventhub_runtime({"MSG1": _graph_message("MSG1")}, sink)
    ckpt = _Checkpointer()
    event = _EHEvent(_eventhub_notification("MSG1"))

    rt.process_batch([event], checkpointer=ckpt)

    assert len(sink.events) == 1
    assert sink.events[0].email.subject == "Invoice #42"
    assert sink.events[0].email.from_.address == "alice@partner.com"
    assert sink.events[0].idempotency_key == f"{TENANT}|{MBX}|MSG1"
    assert ckpt.updated == [event]                       # checkpoint AFTER a successful run


def test_eventhub_duplicate_delivery_is_deduped():
    sink = MemoryEmitter()
    rt = _eventhub_runtime({"MSG1": _graph_message("MSG1")}, sink)
    ckpt = _Checkpointer()
    e1, e2 = _EHEvent(_eventhub_notification("MSG1")), _EHEvent(_eventhub_notification("MSG1"))

    rt.process_batch([e1, e2], checkpointer=ckpt)        # same message twice in one batch

    assert len(sink.events) == 1                         # emitted exactly once
    assert ckpt.updated == [e1, e2]                      # both checkpointed


# =========================================================================== Service Bus lane


def test_servicebus_transport_emits_end_to_end_and_completes():
    sink = MemoryEmitter()
    rt = _servicebus_runtime({"MSG1": _graph_message("MSG1")}, sink)
    receiver = _Receiver()
    msg = _SbMsg(_servicebus_message("MSG1"))

    rt.process_messages([msg], receiver=receiver)

    assert len(sink.events) == 1
    assert sink.events[0].email.subject == "Invoice #42"
    assert sink.events[0].idempotency_key == f"{TENANT}|{MBX}|MSG1"
    assert receiver.completed == [msg]                   # complete AFTER a successful run


def test_servicebus_duplicate_delivery_is_deduped():
    sink = MemoryEmitter()
    rt = _servicebus_runtime({"MSG1": _graph_message("MSG1")}, sink)
    receiver = _Receiver()
    m1, m2 = _SbMsg(_servicebus_message("MSG1")), _SbMsg(_servicebus_message("MSG1"))

    rt.process_messages([m1], receiver=receiver)
    rt.process_messages([m2], receiver=receiver)

    assert len(sink.events) == 1
    assert receiver.completed == [m1, m2]


# =========================================================================== the parity proof


def test_both_transports_produce_identical_clean_email():
    """The load-bearing check: the SAME inbound mail, delivered over Event Hubs vs over
    Service Bus, yields a byte-identical CleanEmail. The transport is invisible downstream."""
    msg = _graph_message("MSG1", subject="Quarterly report")

    eh_sink = MemoryEmitter()
    _eventhub_runtime({"MSG1": msg}, eh_sink).process_batch(
        [_EHEvent(_eventhub_notification("MSG1"))], checkpointer=_Checkpointer()
    )

    sb_sink = MemoryEmitter()
    _servicebus_runtime({"MSG1": msg}, sb_sink).process_messages(
        [_SbMsg(_servicebus_message("MSG1"))], receiver=_Receiver()
    )

    assert len(eh_sink.events) == 1 and len(sb_sink.events) == 1
    eh_email = eh_sink.events[0].email.model_dump()
    sb_email = sb_sink.events[0].email.model_dump()
    assert eh_email == sb_email                           # identical normalized output

    # and the wire envelope agrees on the identity/routing keys too
    assert eh_sink.events[0].idempotency_key == sb_sink.events[0].idempotency_key
    assert eh_sink.events[0].ordering_key == sb_sink.events[0].ordering_key == MBX


def test_both_transports_agree_on_multiple_messages():
    msgs = {"A1": _graph_message("A1", subject="one"), "A2": _graph_message("A2", subject="two")}

    eh_sink = MemoryEmitter()
    eh_rt = _eventhub_runtime(msgs, eh_sink)
    eh_rt.process_batch([_EHEvent(_eventhub_notification("A1"))], checkpointer=_Checkpointer())
    eh_rt.process_batch([_EHEvent(_eventhub_notification("A2"))], checkpointer=_Checkpointer())

    sb_sink = MemoryEmitter()
    sb_rt = _servicebus_runtime(msgs, sb_sink)
    sb_rt.process_messages([_SbMsg(_servicebus_message("A1"))], receiver=_Receiver())
    sb_rt.process_messages([_SbMsg(_servicebus_message("A2"))], receiver=_Receiver())

    assert sorted(e.email.subject for e in eh_sink.events) == ["one", "two"]
    assert sorted(e.email.subject for e in sb_sink.events) == ["one", "two"]
    # per-message parity
    eh_by_id = {e.email.provider_message_id: e.email.model_dump() for e in eh_sink.events}
    sb_by_id = {e.email.provider_message_id: e.email.model_dump() for e in sb_sink.events}
    assert eh_by_id == sb_by_id


def test_both_transports_ignore_forged_client_state():
    """Security parity: a wrong clientState is dropped on both transports (no emit)."""
    eh_sink = MemoryEmitter()
    _eventhub_runtime({"MSG1": _graph_message()}, eh_sink).process_batch(
        [_EHEvent(_eventhub_notification("MSG1", client_state="forged"))], checkpointer=_Checkpointer()
    )
    sb_sink = MemoryEmitter()
    _servicebus_runtime({"MSG1": _graph_message()}, sb_sink).process_messages(
        [_SbMsg(_servicebus_message("MSG1", client_state="forged"))], receiver=_Receiver()
    )
    assert eh_sink.events == []
    assert sb_sink.events == []
