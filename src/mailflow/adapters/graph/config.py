"""Typed config for the Graph + Event Hubs adapter. Secrets are *references*
resolved later via the SecretProvider port — never literal secrets here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EventHubConfig(BaseModel):
    namespace: str                      # e.g. "evh-graphevents" (no .servicebus suffix)
    hub: str                            # e.g. "graph-notifications"
    tenant_domain: str                  # primary domain, e.g. "acme.com"
    consumer_group: str = "$Default"
    checkpoint_blob_ref: str = ""       # ref to blob checkpoint store conn (live wiring)

    @property
    def notification_url(self) -> str:
        # RBAC form (SAS is deprecated): Graph publishes here as the
        # Change Tracking SP granted "Azure Event Hubs Data Sender".
        return (
            f"EventHub:https://{self.namespace}.servicebus.windows.net/"
            f"eventhubname/{self.hub}?tenantId={self.tenant_domain}"
        )


class GraphConfig(BaseModel):
    tenant_id: str
    client_id: str
    client_secret_ref: str              # SecretProvider ref, NOT the secret
    mailboxes: list[str]                # user ids / UPNs to watch
    folders: list[str] = Field(default_factory=lambda: ["inbox", "sentitems"])
    base_url: str = "https://graph.microsoft.com/v1.0"
    scope: str = "https://graph.microsoft.com/.default"
    delivery: Literal["eventhub", "webhook"] = "eventhub"
    client_state: str = "mailflow"      # subscription tripwire (<=128 chars)
    subscription_minutes: int = 8640    # ~6 days; under the 10,080 (7d) max
    max_attempts: int = 3

    @field_validator("mailboxes")
    @classmethod
    def _at_least_one_mailbox(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one mailbox is required")
        return v

    @field_validator("client_state")
    @classmethod
    def _client_state_len(cls, v: str) -> str:
        if len(v) > 128:
            raise ValueError("client_state must be <= 128 chars")
        return v
