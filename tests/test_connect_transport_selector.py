"""The connect() transport selector: delivery='servicebus' vs 'eventhub'.
All tests monkeypatch the transports' run_service, so no azure SDK / network is used."""

import pytest

from mailflow import connect

_APP = {
    "tenant_id": "t", "client_id": "c", "client_secret_ref": "env://SECRET",
    "mailboxes": ["ops@acme.com"],
}
_SB = {**_APP, "fully_qualified_namespace": "ns.servicebus.windows.net",
       "entity_name": "mailflow-graph", "connection_string": "Endpoint=sb://x"}
_EH = {**_APP, "namespace": "evh", "hub": "graph-notifications", "tenant_domain": "acme.com"}


def _spies(monkeypatch):
    calls: dict[str, dict] = {}
    monkeypatch.setattr("mailflow.adapters.servicebus.live.run_service",
                        lambda **kw: calls.__setitem__("sb", kw))
    monkeypatch.setattr("mailflow.adapters.graph.live.run_service",
                        lambda **kw: calls.__setitem__("eh", kw))
    return calls


def test_delivery_servicebus_dispatches_to_service_bus(monkeypatch):
    calls = _spies(monkeypatch)
    mf = connect("graph", delivery="servicebus", credentials=_SB, tenant="acme")
    mf.run()                                   # invokes the live closure -> patched run_service
    assert "sb" in calls and "eh" not in calls
    assert calls["sb"]["servicebus_cfg"].entity_name == "mailflow-graph"
    assert calls["sb"]["servicebus_cfg"].fully_qualified_namespace == "ns.servicebus.windows.net"
    assert calls["sb"]["graph_cfg"].client_id == "c"
    assert calls["sb"]["tenant"] == "acme"
    assert calls["sb"]["connection_string"] == "Endpoint=sb://x"


def test_default_delivery_is_eventhub(monkeypatch):
    calls = _spies(monkeypatch)
    mf = connect("graph", credentials=_EH, tenant="acme")   # no delivery= -> eventhub
    mf.run()
    assert "eh" in calls and "sb" not in calls
    assert calls["eh"]["eventhub"].hub == "graph-notifications"


def test_explicit_delivery_eventhub(monkeypatch):
    calls = _spies(monkeypatch)
    connect("graph", delivery="eventhub", credentials=_EH, tenant="acme").run()
    assert "eh" in calls and "sb" not in calls


def test_invalid_delivery_raises_value_error():
    with pytest.raises(ValueError, match="delivery"):
        connect("graph", delivery="kafka", credentials=_SB)   # type: ignore[arg-type]


def test_servicebus_delivery_threads_on_filtered(monkeypatch):
    calls = _spies(monkeypatch)
    connect("graph", delivery="servicebus", credentials=_SB, tenant="acme",
            on_filtered="drop").run()
    assert calls["sb"]["on_filtered"] == "drop"
