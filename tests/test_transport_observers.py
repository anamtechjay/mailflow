"""The Graph Event Hubs and Service Bus live runtimes must deliver on_trace to the
consumer, same as the memory path. Drives the real pipeline via fakes (no Azure SDK)."""
import json
from typing import Any

from mailflow.adapters.graph.composition import build_graph_runtime
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.core.observability import Observers
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import (
    InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore,
)

MBX = "ops@acme.com"


class _Tok:
    def get_token(self) -> str: return "t"


class _Resp:
    def __init__(self, code, payload): self.status_code, self._p, self.headers, self.content = code, payload, {}, b""
    def json(self): return self._p


class _Transport:
    def __init__(self, msg): self.msg = msg
    def request(self, method, url, *, headers, json):
        if "/attachments" in url: return _Resp(200, {"value": []})
        if "/messages/" in url: return _Resp(200, self.msg)
        return _Resp(200, {"value": []})


class _EHEvent:
    def __init__(self, body): self._b = body
    def body_as_str(self): return self._b


class _Ckpt:
    def update(self, e): ...


def _msg():
    return {"id": "MSG1", "internetMessageId": "<m@x>", "subject": "s",
            "from": {"emailAddress": {"address": "a@x.com"}},
            "toRecipients": [{"emailAddress": {"address": MBX}}], "ccRecipients": [],
            "body": {"contentType": "text", "content": "b"}, "bodyPreview": "b",
            "receivedDateTime": "2026-07-06T10:00:00Z", "sentDateTime": "2026-07-06T09:00:00Z",
            "isDraft": False, "hasAttachments": False, "parentFolderId": "inbox", "categories": []}


def _note():
    return json.dumps({"value": [{"subscriptionId": "s", "changeType": "created",
        "clientState": "mailflow", "resource": f"Users/{MBX}/Messages/MSG1",
        "resourceData": {"id": "MSG1"}}]})


def test_graph_eventhub_runtime_delivers_on_trace():
    traces = []
    rt = build_graph_runtime(
        graph_cfg=GraphConfig(tenant_id="t", client_id="c", client_secret_ref="env://X", mailboxes=[MBX]),
        eventhub=EventHubConfig(namespace="e", hub="h", tenant_domain="acme.com"),
        tenant="acme", token_provider=_Tok(), transport=_Transport(_msg()),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), observers=Observers(on_trace=traces.append),
    )
    rt.process_batch([_EHEvent(_note())], checkpointer=_Ckpt())
    assert [t.disposition.value for t in traces] == ["emitted"]
