"""Microsoft Graph + Azure Event Hubs adapter (app-only, no webhook)."""

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.graph.costs import CostSummary, CostTracker
from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.lifecycle import GraphLifecycleHandler
from mailflow.adapters.graph.notifications import (
    GraphLifecycleEvent,
    GraphNotification,
    parse_lifecycle_payload,
    parse_notification_payload,
)
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.adapters.graph.provider import GraphProvider
from mailflow.adapters.graph.reconciler import SubscriptionReconciler
from mailflow.adapters.graph.runtime import EventHubCheckpointer, GraphEventHubsRuntime
from mailflow.adapters.graph.subscriptions import GraphSubscriptionManager

__all__ = [
    "GraphClient", "GraphConfig", "EventHubConfig", "GraphExtractor",
    "GraphNotification", "parse_notification_payload", "GraphEnvelopeParser",
    "GraphProvider", "GraphEventHubsRuntime", "EventHubCheckpointer",
    "GraphSubscriptionManager", "GraphLifecycleEvent", "parse_lifecycle_payload",
    "GraphLifecycleHandler", "SubscriptionReconciler", "CostTracker", "CostSummary",
]
