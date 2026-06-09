"""Layered loader + validate (spec §11). YAML support uses pydantic + stdlib only
where possible; if PyYAML is unavailable we accept JSON. (Core spine keeps deps light.)"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mailflow.config.schema import MailflowConfig
from mailflow.core.errors import ConfigError
from mailflow.registry import (
    EMITTER_KINDS,
    FILTER_KINDS,
    PROVIDER_KINDS,
    STORE_KINDS,
)


def _parse(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]

        return dict(yaml.safe_load(text) or {})
    except ModuleNotFoundError:
        return dict(json.loads(text))  # fall back to JSON if PyYAML not installed


def load_config(path: str) -> MailflowConfig:
    data = _parse(Path(path).read_text())
    # env overlay: MAILFLOW_TENANT overrides tenant (spec §11 precedence)
    if "MAILFLOW_TENANT" in os.environ:
        data["tenant"] = os.environ["MAILFLOW_TENANT"]
    return MailflowConfig.model_validate(data)


def validate(cfg: MailflowConfig) -> list[str]:
    """Raise ConfigError on unknown kinds; return non-fatal reachability warnings."""
    if cfg.provider.kind not in PROVIDER_KINDS:
        raise ConfigError(f"unknown provider kind {cfg.provider.kind!r}")
    if cfg.emitter.kind not in EMITTER_KINDS:
        raise ConfigError(f"unknown emitter kind {cfg.emitter.kind!r}")
    for store in (cfg.stores.cursor, cfg.stores.dedupe, cfg.stores.blob):
        if store.kind not in STORE_KINDS:
            raise ConfigError(f"unknown store kind {store.kind!r}")
    for f in cfg.filters:
        if f.kind not in FILTER_KINDS:
            raise ConfigError(f"unknown filter kind {f.kind!r}")

    warnings: list[str] = []
    seen_catchall_keep = False
    for f in cfg.filters:
        if seen_catchall_keep and f.params.get("on_match", "drop") == "drop":
            warnings.append(f"filter {f.kind!r} is unreachable after a catch-all keep")
        if f.kind == "whitelist" and not f.params.get("domains"):
            seen_catchall_keep = True  # empty whitelist would keep nothing; placeholder rule
    return warnings
