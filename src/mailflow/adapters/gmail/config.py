"""Typed config for the Gmail + Pub/Sub adapter. Secrets are *references* resolved
later via the SecretProvider port — never literal secrets here."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"


class PubSubConfig(BaseModel):
    project_id: str
    topic: str                       # short name, e.g. "gmail-notifications"
    subscription: str                # short name, e.g. "mailflow"

    @property
    def topic_path(self) -> str:
        return f"projects/{self.project_id}/topics/{self.topic}"

    @property
    def subscription_path(self) -> str:
        return f"projects/{self.project_id}/subscriptions/{self.subscription}"


class GmailConfig(BaseModel):
    client_id: str
    client_secret_ref: str           # SecretProvider ref, NOT the secret
    oauth_refresh_token_ref: str     # SecretProvider ref to the per-user refresh token
    mailboxes: list[str]             # user ids / addresses to watch ("me" or the address)
    label_ids: list[str] = Field(default_factory=lambda: ["INBOX"])
    scopes: list[str] = Field(default_factory=lambda: [GMAIL_READONLY])
    base_url: str = "https://gmail.googleapis.com/gmail/v1"
    token_uri: str = "https://oauth2.googleapis.com/token"
    max_attempts: int = 3
    # reliability (spec: keep the long-running service alive). 0 disables a feature.
    watch_renew_seconds: int = 86400   # renew the watch daily (well under the ~7-day expiry)
    sweep_seconds: int = 900           # safety-net poll every 15 min

    @field_validator("mailboxes")
    @classmethod
    def _at_least_one_mailbox(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one mailbox is required")
        return v
