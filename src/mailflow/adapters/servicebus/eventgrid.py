"""Unwrap Event Grid CloudEvents (delivered via a Service Bus queue) into the existing
GraphNotification pointers. The Service Bus message body is a single CloudEvent JSON
object or a JSON array of them; each event's `data` holds the Graph change-notification
fields. We normalize each event's `data` into the item shape the battle-tested
graph.notifications.parse_notification_payload expects, then delegate — reusing its
resource regex, validation-event skip, and constant-time clientState check (DRY)."""

from __future__ import annotations

import json
from typing import Any

from mailflow.adapters.graph.notifications import GraphNotification, parse_notification_payload


def split_cloudevents(body: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _to_item(event: dict[str, Any]) -> dict[str, Any]:
    """Map one CloudEvent to a Graph changeNotification 'value' item. Prefer the `data`
    object's fields; fall back to the CloudEvent `subject` for the resource path.
    A malformed event's `data` may be a non-dict (list/str/int/None); treat that as
    empty rather than raising, so one poison message can't crash-loop the consumer."""
    data = event.get("data")
    if not isinstance(data, dict):
        data = {}
    resource = str(data.get("resource") or event.get("subject") or "")
    return {
        "subscriptionId": data.get("subscriptionId", ""),
        "changeType": data.get("changeType", ""),
        "clientState": data.get("clientState", ""),
        "resource": resource,
        "resourceData": data.get("resourceData") or {},
    }


def parse_eventgrid_message(
    body: str, *, expected_client_state: str
) -> list[GraphNotification]:
    items = [_to_item(e) for e in split_cloudevents(body)]
    if not items:
        return []
    collection = json.dumps({"value": items})
    return parse_notification_payload(collection, expected_client_state=expected_client_state)
