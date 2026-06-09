import mailflow


def test_public_api_exports():
    for name in (
        "__version__", "CleanEmail", "EmailEvent", "SCHEMA_VERSION",
        "Pipeline", "build_from_config", "MailflowConfig",
    ):
        assert hasattr(mailflow, name), name


def test_schema_version_is_one_point_zero():
    assert mailflow.SCHEMA_VERSION == "1.0"
