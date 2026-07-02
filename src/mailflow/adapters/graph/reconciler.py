"""SubscriptionReconciler — keep Graph subscriptions matching the desired set
(config.mailboxes x config.folders). reconcile() creates the missing ones and
tracks existing ones (matched by our Event Hub notificationUrl so we never touch
another app's subscriptions); renew_all() PATCHes each tracked subscription's
expiry on a timer tick (recreate-on-404 is handled by the subscription manager).
"""

from __future__ import annotations

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.graph.subscriptions import GraphSubscriptionManager, folder_resource
from mailflow.core.models import StreamRef


class SubscriptionReconciler:
    def __init__(
        self, *, client: GraphClient, subscription_manager: GraphSubscriptionManager,
        config: GraphConfig, eventhub: EventHubConfig,
    ) -> None:
        self.client = client
        self.subscription_manager = subscription_manager
        self.config = config
        self.eventhub = eventhub
        self._handles: dict[str, str] = {}  # stream.key -> subscription handle

    def desired_streams(self) -> list[StreamRef]:
        return [
            StreamRef(mailbox=mailbox, folder=folder)
            for mailbox in self.config.mailboxes
            for folder in self.config.folders
        ]

    def reconcile(self) -> dict[str, str]:
        actual: dict[str, str] = {}  # resource -> subscription id (ours only)
        for sub in self.client.list_subscriptions():
            if str(sub.get("notificationUrl", "")) == self.eventhub.notification_url:
                actual[str(sub.get("resource", ""))] = str(sub.get("id", ""))

        handles: dict[str, str] = {}
        for stream in self.desired_streams():
            resource = folder_resource(stream)
            existing = actual.get(resource)
            if existing is not None:
                self.subscription_manager.register(existing, stream)
                handles[stream.key] = existing
            else:
                handles[stream.key] = str(self.subscription_manager.ensure_watch(stream))
        self._handles = handles
        return handles

    def renew_all(self) -> None:
        renewed: dict[str, str] = {}
        for stream_key, handle in self._handles.items():
            renewed[stream_key] = str(self.subscription_manager.renew_watch(handle))
        self._handles = renewed
