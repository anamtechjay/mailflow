"""A11 — MailflowConfig carries a config-schema version field (Phase 0 contract)."""

from __future__ import annotations

from mailflow.config.schema import MailflowConfig


def test_config_has_version_default_1_0() -> None:
    assert MailflowConfig().version == "1.0"


def test_config_version_round_trips() -> None:
    assert MailflowConfig(version="1.0").version == "1.0"
