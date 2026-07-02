"""Parse an Event Hub event body (Graph changeNotificationCollection) into thin
pointers. Skips the validation event (subscriptionId == 'NA') and drops any item
whose clientState does not match (constant-time compare)."""

from __future__ import annotations

import hmac
import json
import re
from typing import Any

from pydantic import BaseModel

from mailflow.core.models import StreamRef

_RESOURCE_RE = re.compile(r"users/(?P<user>[^/]+)/messages/(?P<msg>[^/?]+)", re.IGNORECASE)
_FOLDER_RESOURCE_RE = re.compile(
    r"users/(?P<user>[^/]+)/mailFolders\('(?P<folder>[^']+)'\)", re.IGNORECASE
)

_LIFECYCLE_EVENTS = frozenset(
    {"reauthorizationRequired", "subscriptionRemoved", "missed"}
)


class GraphNotification(BaseModel):
    subscription_id: str
    change_type: str
    user_id: str
    message_id: str


class GraphLifecycleEvent(BaseModel):
    """A subscription lifecycle signal delivered on the same hub as message
    notifications (reauthorizationRequired / subscriptionRemoved / missed)."""

    subscription_id: str
    lifecycle_event: str
    stream: StreamRef | None = None


def _stream_from_folder_resource(resource: str) -> StreamRef | None:
    m = _FOLDER_RESOURCE_RE.search(resource)
    if not m:
        return None
    return StreamRef(mailbox=m.group("user"), folder=m.group("folder"))


def parse_lifecycle_payload(
    body: str, *, expected_client_state: str
) -> list[GraphLifecycleEvent]:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    out: list[GraphLifecycleEvent] = []
    for item in data.get("value", []):
        event = str(item.get("lifecycleEvent", ""))
        if event not in _LIFECYCLE_EVENTS:
            continue  # not a lifecycle item (message notification or validation)
        if not hmac.compare_digest(str(item.get("clientState", "")), expected_client_state):
            continue  # forged / mismatched — drop, never log clientState
        out.append(GraphLifecycleEvent(
            subscription_id=str(item.get("subscriptionId", "")),
            lifecycle_event=event,
            stream=_stream_from_folder_resource(str(item.get("resource", ""))),
        ))
    return out


def _user_and_message(item: dict[str, Any]) -> tuple[str, str] | None:
    resource = str(item.get("resource", ""))
    m = _RESOURCE_RE.search(resource)
    msg_id = str((item.get("resourceData") or {}).get("id", "")) or (m.group("msg") if m else "")
    if not m and not msg_id:
        return None
    user = m.group("user") if m else ""
    if not user or not msg_id:
        return None
    return user, msg_id


def parse_notification_payload(body: str, *, expected_client_state: str) -> list[GraphNotification]:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    out: list[GraphNotification] = []
    for item in data.get("value", []):
        if str(item.get("subscriptionId")) == "NA":
            continue  # validation event — ignore per Event Hubs delivery docs
        if not hmac.compare_digest(str(item.get("clientState", "")), expected_client_state):
            continue  # forged / mismatched — drop, never log clientState
        ids = _user_and_message(item)
        if ids is None:
            continue
        user, msg = ids
        out.append(GraphNotification(
            subscription_id=str(item.get("subscriptionId", "")),
            change_type=str(item.get("changeType", "")),
            user_id=user,
            message_id=msg,
        ))
    return out
