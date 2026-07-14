from mailflow.adapters.graph.config import GraphConfig
from mailflow.adapters.servicebus.composition import build_servicebus_graph_runtime
from mailflow.adapters.servicebus.runtime import ServiceBusRuntime
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore


class _FakeToken:
    def get_token(self): return "t"


class _FakeTransport:
    def request(self, method, url, *, headers, json): raise AssertionError("no network in unit test")


def test_builds_a_servicebus_runtime_wired_to_a_graph_pipeline():
    rt = build_servicebus_graph_runtime(
        graph_cfg=GraphConfig(
            tenant_id="t", client_id="c", client_secret_ref="env://X", mailboxes=["ops@acme.com"]
        ),
        tenant="acme",
        token_provider=_FakeToken(),
        transport=_FakeTransport(),
        emitter=MemoryEmitter(),
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
    )
    assert isinstance(rt, ServiceBusRuntime)
    assert rt.client_state == "mailflow"   # from GraphConfig default
