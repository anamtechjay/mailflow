from mailflow.registry import (
    EMITTER_KINDS,
    FILTER_KINDS,
    PROVIDER_KINDS,
    STORE_KINDS,
    build_filter,
)


def test_builtin_kinds_registered():
    assert "memory" in PROVIDER_KINDS
    assert {"memory", "stdout"} <= set(EMITTER_KINDS)
    assert {"whitelist", "blacklist", "subject", "list_mail", "internal_domain"} <= set(FILTER_KINDS)
    assert "memory" in STORE_KINDS


def test_build_filter_constructs_with_params():
    f = build_filter("whitelist", {"domains": ["partner.com"]})
    assert f.name == "whitelist"
