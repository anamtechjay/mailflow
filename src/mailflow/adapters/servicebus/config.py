"""Typed config for the Service Bus adapter. Secrets are *references* resolved
later via the SecretProvider port — never literal secrets here."""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class ServiceBusConfig(BaseModel):
    fully_qualified_namespace: str      # "<namespace>.servicebus.windows.net"
    entity_name: str                    # queue or topic name
    connection_string_ref: str = ""     # SecretProvider ref (SAS) — or "" for RBAC/credential

    @field_validator("entity_name")
    @classmethod
    def _entity_required(cls, v: str) -> str:
        if not v:
            raise ValueError("entity_name (queue or topic) is required")
        return v
