"""GmailWatchManager — implements the SubscriptionManager port for Gmail. ensure_watch
calls users.watch(topic, labelIds) which returns the starting historyId (seed the
cursor with it); renew_watch re-watches (Gmail watches expire in ~7 days, renew daily).
There is no PATCH/renew endpoint — renewal is just calling watch again."""

from __future__ import annotations

from dataclasses import dataclass

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.core.models import StreamRef


@dataclass(frozen=True)
class WatchHandle:
    mailbox: str
    history_id: str
    expiration: str = ""


class GmailWatchManager:
    def __init__(self, *, client: GmailClient, config: GmailConfig, pubsub: PubSubConfig) -> None:
        self.client = client
        self.config = config
        self.pubsub = pubsub

    def ensure_watch(self, stream: StreamRef) -> WatchHandle:
        out = self.client.watch(stream.mailbox, self.pubsub.topic_path, self.config.label_ids)
        return WatchHandle(
            mailbox=stream.mailbox,
            history_id=str(out.get("historyId", "")),
            expiration=str(out.get("expiration", "")),
        )

    def renew_watch(self, handle: WatchHandle) -> WatchHandle:
        return self.ensure_watch(StreamRef(mailbox=handle.mailbox, folder=None))

    def stop(self, handle: WatchHandle) -> None:
        self.client.stop(handle.mailbox)
