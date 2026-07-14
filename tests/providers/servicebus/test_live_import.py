def test_live_module_imports_without_servicebus_extra():
    # All azure imports must be local to functions, so importing the module must NOT
    # require azure-servicebus to be installed.
    import importlib

    mod = importlib.import_module("mailflow.adapters.servicebus.live")
    assert hasattr(mod, "run_service")
    assert hasattr(mod, "AzureServiceBusSender")
