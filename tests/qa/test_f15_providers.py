"""F15 — Providers & transports (integration). See docs/qa-partA-coverage.md.

Drives the Graph provider stack (GraphProvider -> Pipeline -> transport runtime) over
the fake Graph HTTP transport + synthetic notifications, for both delivery transports
(Azure Event Hubs and Event Grid -> Service Bus). Mirrors
tests/test_transport_parity_e2e.py and tests/providers/servicebus/test_e2e.py.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mailflow.adapters.graph.composition import build_graph_runtime
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.servicebus.composition import build_servicebus_graph_runtime
from mailflow.core.models import StreamRef
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.fakes import _FakeGraphTransport, _graph_message

TENANT = "acme"
MBX = "ops@acme.com"
STREAM = StreamRef(mailbox=MBX, folder="inbox")


# --------------------------------------------------------------------------- transport fakes


class _FakeToken:
    def get_token(self) -> str:
        return "tok"


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


def _eventhub_notification(msg_id: str = "MSG1", *, client_state: str = "mailflow") -> str:
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


def _eventhub_runtime(
    messages: dict[str, dict[str, Any]], emitter: Any, *, errors: dict[str, int] | None = None
) -> Any:
    return build_graph_runtime(
        graph_cfg=GraphConfig(tenant_id="t", client_id="c", client_secret_ref="env://X", mailboxes=[MBX]),
        eventhub=EventHubConfig(namespace="evh", hub="graph-notifications", tenant_domain="acme.com"),
        tenant=TENANT,
        token_provider=_FakeToken(),
        transport=_FakeGraphTransport(messages, errors=errors),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
    )


def _servicebus_runtime(
    messages: dict[str, dict[str, Any]], emitter: Any, *, errors: dict[str, int] | None = None
) -> Any:
    return build_servicebus_graph_runtime(
        graph_cfg=GraphConfig(tenant_id="t", client_id="c", client_secret_ref="env://X", mailboxes=[MBX]),
        tenant=TENANT,
        token_provider=_FakeToken(),
        transport=_FakeGraphTransport(messages, errors=errors),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
    )


# =========================================================================== forged clientState


def test_forged_client_state_dropped() -> None:
    """A wrong clientState is dropped on BOTH transports: the notification carries no
    trusted items, so the pipeline never runs and nothing is emitted (but the
    transport still acks/checkpoints -- a forged notification isn't a delivery
    failure to redrive)."""
    eh_sink = MemoryEmitter()
    ckpt = _Checkpointer()
    _eventhub_runtime({"MSG1": _graph_message("MSG1")}, eh_sink).process_batch(
        [_EHEvent(_eventhub_notification("MSG1", client_state="forged"))], checkpointer=ckpt
    )
    assert eh_sink.events == []
    assert ckpt.updated  # checkpointed -- nothing to redeliver

    sb_sink = MemoryEmitter()
    receiver = _Receiver()
    _servicebus_runtime({"MSG1": _graph_message("MSG1")}, sb_sink).process_messages(
        [_SbMsg(_servicebus_message("MSG1", client_state="forged"))], receiver=receiver
    )
    assert sb_sink.events == []
    assert receiver.completed
    assert receiver.abandoned == []


# =========================================================================== fetch edges


def test_404_graceful() -> None:
    """Message deleted before fetch (Graph GET 404): graceful skip, no emit, no crash,
    message completed (not abandoned)."""
    sink = MemoryEmitter()
    receiver = _Receiver()
    _servicebus_runtime({}, sink, errors={"MSG1": 404}).process_messages(
        [_SbMsg(_servicebus_message("MSG1"))], receiver=receiver
    )

    assert sink.events == []
    assert receiver.completed
    assert receiver.abandoned == []


def test_500_abandon_reraise() -> None:
    """Infra error (Graph GET 500): run_once() raises -> the SB message is abandoned
    for redelivery and the exception re-raised (CD-1); NOT completed."""
    sink = MemoryEmitter()
    rt = _servicebus_runtime({}, sink, errors={"MSG1": 500})
    receiver = _Receiver()
    msg = _SbMsg(_servicebus_message("MSG1"))

    with pytest.raises(Exception):
        rt.process_messages([msg], receiver=receiver)

    assert sink.events == []
    assert receiver.abandoned == [msg]
    assert receiver.completed == []


# =========================================================================== parity


def test_eventhub_servicebus_parity() -> None:
    """The SAME inbound mail delivered over Event Hubs vs Service Bus yields an
    identical CleanEmail -- the transport choice is invisible downstream."""
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
    assert eh_sink.events[0].email.model_dump() == sb_sink.events[0].email.model_dump()
    assert eh_sink.events[0].idempotency_key == sb_sink.events[0].idempotency_key
    assert eh_sink.events[0].ordering_key == sb_sink.events[0].ordering_key == MBX


# =========================================================================== empty batch


def test_empty_batch() -> None:
    """An empty batch/list of messages is a no-op on both transports: nothing
    emitted, nothing acked/checkpointed, no crash."""
    eh_sink = MemoryEmitter()
    ckpt = _Checkpointer()
    _eventhub_runtime({}, eh_sink).process_batch([], checkpointer=ckpt)
    assert eh_sink.events == []
    assert ckpt.updated == []

    sb_sink = MemoryEmitter()
    receiver = _Receiver()
    _servicebus_runtime({}, sb_sink).process_messages([], receiver=receiver)
    assert sb_sink.events == []
    assert receiver.completed == []
    assert receiver.abandoned == []
