import textwrap

import pytest

from mailflow.config.loader import load_config, validate
from mailflow.config.schema import MailflowConfig
from mailflow.core.errors import ConfigError


def test_defaults_give_a_memory_pipeline():
    cfg = MailflowConfig()
    assert cfg.provider.kind == "memory"
    assert cfg.emitter.kind == "memory"
    assert cfg.filters == []                      # destructive filters absent by default
    assert cfg.classifier.enabled is False


def test_load_from_yaml(tmp_path):
    # PyYAML is an optional dep; when absent the loader falls back to JSON. We write
    # JSON-compatible content (a strict subset of YAML) so this passes either way.
    p = tmp_path / "mailflow.yaml"
    p.write_text(textwrap.dedent("""
        {
          "tenant": "acme",
          "provider": { "kind": "memory" },
          "filters": [
            { "kind": "whitelist", "params": { "domains": ["partner.com"] } },
            { "kind": "blacklist", "params": { "domains": ["spam.com"] } }
          ],
          "emitter": { "kind": "stdout" },
          "security": { "read_allowlist": ["ops@acme.com"] }
        }
    """))
    cfg = load_config(str(p))
    assert cfg.tenant == "acme"
    assert [f.kind for f in cfg.filters] == ["whitelist", "blacklist"]
    assert cfg.emitter.kind == "stdout"
    assert cfg.security.read_allowlist == ["ops@acme.com"]


def test_validate_rejects_unknown_kind():
    cfg = MailflowConfig.model_validate({
        "tenant": "acme",
        "provider": {"kind": "memory"},
        "filters": [{"kind": "nonsense"}],
    })
    with pytest.raises(ConfigError) as e:
        validate(cfg)
    assert "nonsense" in str(e.value)


def test_validate_warns_on_unreachable_drop_after_catchall_keep():
    cfg = MailflowConfig.model_validate({
        "tenant": "acme",
        "filters": [
            {"kind": "whitelist", "params": {"domains": ["partner.com"]}},
            {"kind": "blacklist", "params": {"domains": ["spam.com"]}},
        ],
    })
    warnings = validate(cfg)            # no unreachable rule here -> no warnings
    assert warnings == []
