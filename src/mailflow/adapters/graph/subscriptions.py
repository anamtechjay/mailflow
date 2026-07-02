"""GraphSubscriptionManager — implements the core SubscriptionManager port for
Event Hubs delivery. ensure_watch creates a per-folder subscription whose
notificationUrl points at the Event Hub; renew_watch PATCHes the expiry and
recreates the subscription if Graph returns 404 (subscription gone)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.graph.transport import GraphError
from mailflow.core.models import StreamRef


def _default_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def folder_resource(stream: StreamRef) -> str:
    """The Graph subscription resource path for a mailbox folder stream. Shared by
    the subscription manager and the reconciler so the two agree exactly."""
    folder = stream.folder or "inbox"
    return f"users/{stream.mailbox}/mailFolders('{folder}')/messages"


class GraphSubscriptionManager:
    def __init__(
        self, *, client: GraphClient, config: GraphConfig, eventhub: EventHubConfig,
        now_iso: Callable[[], str] = _default_now_iso,
    ) -> None:
        self.client = client
        self.config = config
        self.eventhub = eventhub
        self._now_iso = now_iso
        self._stream_by_handle: dict[str, StreamRef] = {}

    def _resource(self, stream: StreamRef) -> str:
        return folder_resource(stream)

    def register(self, handle: object, stream: StreamRef) -> None:
        """Record a handle -> stream mapping for a subscription we did not create
        ourselves (e.g. one discovered by the reconciler), so renew/recreate-on-404
        can resolve its stream."""
        self._stream_by_handle[str(handle)] = stream

    def stream_for_handle(self, handle: object) -> StreamRef | None:
        """Resolve a subscription id to the stream it watches (for the provider's
        folder mapping)."""
        return self._stream_by_handle.get(str(handle))

    def _expiration_iso(self) -> str:
        base = datetime.strptime(self._now_iso(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        exp = base + timedelta(minutes=self.config.subscription_minutes)
        return exp.strftime("%Y-%m-%dT%H:%M:%SZ")

    def ensure_watch(self, stream: StreamRef) -> object:
        out = self.client.create_subscription(
            resource=self._resource(stream),
            notification_url=self.eventhub.notification_url,
            client_state=self.config.client_state,
            expiration_iso=self._expiration_iso(),
        )
        handle = str(out["id"])
        self._stream_by_handle[handle] = stream
        return handle

    def renew_watch(self, handle: object) -> object:
        sub_id = str(handle)
        try:
            self.client.renew_subscription(sub_id, self._expiration_iso())
            return sub_id
        except GraphError as exc:
            if exc.status_code != 404:
                raise
            stream = self._stream_by_handle.get(sub_id)
            if stream is None:
                raise
            new_handle = self.ensure_watch(stream)
            self._stream_by_handle.pop(sub_id, None)
            return new_handle
