from mailflow.adapters.servicebus.config import ServiceBusConfig


def test_config_holds_entity_and_ref_but_not_literal_secret():
    cfg = ServiceBusConfig(
        fully_qualified_namespace="ns.servicebus.windows.net",
        entity_name="mailflow-graph",
        connection_string_ref="env://SB_CONNECTION_STRING",
    )
    assert cfg.entity_name == "mailflow-graph"
    assert cfg.connection_string_ref == "env://SB_CONNECTION_STRING"


def test_entity_name_required():
    import pytest
    with pytest.raises(ValueError):
        ServiceBusConfig(fully_qualified_namespace="ns.servicebus.windows.net", entity_name="")
