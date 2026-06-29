"""Pydantic config models (spec §11). Secrets are references resolved lazily."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ComponentConfig(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class ClassifierConfig(BaseModel):
    enabled: bool = False
    policy: str = "flag"          # flag (default, non-destructive) | drop  (spec OD-2)


class SecurityConfig(BaseModel):
    read_allowlist: list[str] = Field(default_factory=list)   # fail-closed (spec §9.1)
    verify_scope_on_startup: bool = True


class StoresConfig(BaseModel):
    cursor: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    dedupe: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    blob: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))


class MailflowConfig(BaseModel):
    version: str = "1.0"  # config-schema version; fail-fast on unknown major in Phase 1 (A11)
    tenant: str = "default"
    provider: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    filters: list[ComponentConfig] = Field(default_factory=list)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    stores: StoresConfig = Field(default_factory=StoresConfig)
    emitter: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    max_message_bytes: int = 50_000_000
    max_attempts: int = 3
