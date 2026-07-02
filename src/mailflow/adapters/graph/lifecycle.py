"""GraphLifecycleHandler — reacts to subscription lifecycle events so ingestion
doesn't stop silently (spec §8.5). Adapter-internal: the frozen core
SubscriptionManager port has no lifecycle hook, so this lives outside it.

  reauthorizationRequired -> renew (PATCH) the subscription
  subscriptionRemoved     -> recreate (ensure_watch) the stream's subscription
  missed                  -> trigger a delta resync of the affected stream
"""

from __future__ import annotations

from typing import Callable, Protocol

from mailflow.adapters.graph.notifications import GraphLifecycleEvent
from mailflow.core.models import StreamRef


class _SubManagerLike(Protocol):
    def renew_watch(self, handle: object) -> object: ...
    def ensure_watch(self, stream: StreamRef) -> object: ...


class GraphLifecycleHandler:
    def __init__(
        self,
        *,
        subscription_manager: _SubManagerLike,
        resync: Callable[[StreamRef], None] | None = None,
    ) -> None:
        self.subscription_manager = subscription_manager
        self._resync = resync

    def handle(self, event: GraphLifecycleEvent) -> str:
        if event.lifecycle_event == "reauthorizationRequired":
            self.subscription_manager.renew_watch(event.subscription_id)
            return "renewed"
        if event.lifecycle_event == "subscriptionRemoved":
            if event.stream is None:
                return "ignored"
            self.subscription_manager.ensure_watch(event.stream)
            return "recreated"
        if event.lifecycle_event == "missed":
            if self._resync is not None and event.stream is not None:
                self._resync(event.stream)
                return "resynced"
            return "ignored"
        return "ignored"
