"""GraphEnvelopeParser — Graph message JSON (carried in RawMessage.raw_bytes) to
a provider-neutral Envelope. Implements the EnvelopeParser port. No network."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mailflow.core.classification import derive_auto_submitted, derive_is_bounce
from mailflow.core.identity import derive_canonical_id
from mailflow.core.models import Envelope, RawMessage, Recipient


def _recipient(node: dict[str, Any] | None) -> Recipient:
    ea = (node or {}).get("emailAddress", {}) if node else {}
    return Recipient(name=str(ea.get("name", "")), address=str(ea.get("address", "")))


def _recipients(nodes: list[dict[str, Any]] | None) -> list[Recipient]:
    return [_recipient(n) for n in (nodes or []) if (n or {}).get("emailAddress", {}).get("address")]


def _headers(msg: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for h in msg.get("internetMessageHeaders", []) or []:
        name = str(h.get("name", "")).lower()
        if name:
            out.setdefault(name, []).append(str(h.get("value", "")))
    return out


def _first(headers: dict[str, list[str]], name: str) -> str | None:
    vals = headers.get(name)
    return vals[0] if vals else None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class GraphEnvelopeParser:
    def parse_envelope(self, msg: RawMessage, tenant: str) -> Envelope:
        data: dict[str, Any] = json.loads(msg.raw_bytes or b"{}")
        message_id = data.get("internetMessageId")
        canonical_id, present, trusted = derive_canonical_id(
            provider=msg.provider, provider_message_id=msg.provider_message_id,
            mailbox=msg.stream.mailbox, message_id=message_id,
        )
        headers = _headers(data)
        reply_to_list = data.get("replyTo") or []
        from_node = _recipient(data.get("from"))
        auto_sub = _first(headers, "auto-submitted")
        env_kwargs: dict[str, Any] = {
            "canonical_id": canonical_id,
            "message_id": message_id,
            "message_id_present": present,
            "message_id_trusted": trusted,
            "provider": msg.provider,
            "provider_message_id": msg.provider_message_id,
            "stream": msg.stream,
            "from": from_node,
            "sender": _recipient(data.get("sender")) if data.get("sender") else None,
            "reply_to": _recipient(reply_to_list[0]) if reply_to_list else None,
            "to": _recipients(data.get("toRecipients")),
            "cc": _recipients(data.get("ccRecipients")),
            "subject": str(data.get("subject", "")),
            "date_utc": _dt(data.get("sentDateTime")),
            "received_at": _dt(data.get("receivedDateTime")),
            "snippet": str(data.get("bodyPreview", "")),
            "list_id": _first(headers, "list-id"),
            "list_unsubscribe": _first(headers, "list-unsubscribe"),
            "auto_submitted": auto_sub,
            "is_auto_submitted": derive_auto_submitted(auto_sub),
            "is_bounce": derive_is_bounce(
                from_address=from_node.address,
                return_path=_first(headers, "return-path"),
                content_type=_first(headers, "content-type"),
            ),
            "headers": headers,
        }
        return Envelope.model_validate(env_kwargs)
