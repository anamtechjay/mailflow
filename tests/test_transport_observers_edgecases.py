"""Live-transport parity for the observer contract (extends
tests/test_transport_observers.py, which only covers the Event Hubs on_trace path):
the Service Bus runtime must deliver on_trace identically, and both runtimes must
fire on_report once per run_once(). Drives the real pipeline via fakes (no Azure
SDK), reusing the fake shapes from tests/test_transport_parity_e2e.py and
tests/providers/servicebus/test_e2e.py."""

from __future__ import annotations

import json

from mailflow.adapters.graph.composition import build_graph_runtime
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.servicebus.composition import build_servicebus_graph_runtime
from mailflow.core.observability import Observers, RunReport
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import (
    InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore,
)

MBX = "ops@acme.com"


class _Tok:
    def get_token(self) -> str:
        return "t"


class _Resp:
    def __init__(self, code, payload):
        self.status_code, self._p, self.headers, self.content = code, payload, {}, b""

    def json(self):
        return self._p


class _Transport:
    def __init__(self, msg):
        self.msg = msg

    def request(self, method, url, *, headers, json):
        if "/attachments" in url:
            return _Resp(200, {"value": []})
        if "/messages/" in url:
            return _Resp(200, self.msg)
        return _Resp(200, {"value": []})


class _EHEvent:
    def __init__(self, body):
        self._b = body

    def body_as_str(self):
        return self._b


class _Ckpt:
    def update(self, e): ...


class _SbMsg:
    def __init__(self, body):
        self._body = body

    def body_as_str(self):
        return self._body


class _Receiver:
    def __init__(self):
        self.completed = []
        self.abandoned = []

    def complete(self, message):
        self.completed.append(message)

    def abandon(self, message):
        self.abandoned.append(message)


def _msg():
    return {"id": "MSG1", "internetMessageId": "<m@x>", "subject": "s",
            "from": {"emailAddress": {"address": "a@x.com"}},
            "toRecipients": [{"emailAddress": {"address": MBX}}], "ccRecipients": [],
            "body": {"contentType": "text", "content": "b"}, "bodyPreview": "b",
            "receivedDateTime": "2026-07-06T10:00:00Z", "sentDateTime": "2026-07-06T09:00:00Z",
            "isDraft": False, "hasAttachments": False, "parentFolderId": "inbox", "categories": []}


def _eventhub_note():
    return json.dumps({"value": [{"subscriptionId": "s", "changeType": "created",
        "clientState": "mailflow", "resource": f"Users/{MBX}/Messages/MSG1",
        "resourceData": {"id": "MSG1"}}]})


def _servicebus_note():
    return json.dumps({
        "type": "Microsoft.Graph.MessageCreated",
        "subject": f"Users/{MBX}/Messages/MSG1",
        "data": {
            "subscriptionId": "s", "changeType": "created", "clientState": "mailflow",
            "resource": f"Users/{MBX}/Messages/MSG1",
            "resourceData": {"id": "MSG1"},
        },
    })


def test_servicebus_runtime_delivers_on_trace() -> None:
    traces = []
    rt = build_servicebus_graph_runtime(
        graph_cfg=GraphConfig(tenant_id="t", client_id="c", client_secret_ref="env://X",
                               mailboxes=[MBX]),
        tenant="acme", token_provider=_Tok(), transport=_Transport(_msg()),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), observers=Observers(on_trace=traces.append),
    )
    rt.process_messages([_SbMsg(_servicebus_note())], receiver=_Receiver())
    assert [t.disposition.value for t in traces] == ["emitted"]


def test_graph_eventhub_runtime_fires_on_report_per_run_once() -> None:
    reports = []
    rt = build_graph_runtime(
        graph_cfg=GraphConfig(tenant_id="t", client_id="c", client_secret_ref="env://X",
                               mailboxes=[MBX]),
        eventhub=EventHubConfig(namespace="e", hub="h", tenant_domain="acme.com"),
        tenant="acme", token_provider=_Tok(), transport=_Transport(_msg()),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), observers=Observers(on_report=reports.append),
    )
    rt.process_batch([_EHEvent(_eventhub_note())], checkpointer=_Ckpt())
    assert len(reports) == 1
    assert isinstance(reports[0], RunReport)
    assert reports[0].emitted == 1
