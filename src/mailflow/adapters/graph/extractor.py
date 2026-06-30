"""GraphExtractor — Graph message JSON (+ embedded _attachments metadata) to a
CleanEmail. Implements the ContentExtractor port's extract(msg, env) method, the
seam Pipeline._extract dispatches to for non-MIME extractors. No network: the
provider has already fetched the JSON and attachment metadata into raw_bytes.
Attachment BYTES are not downloaded here (metadata only; streaming deferred)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mailflow.core.events import SCHEMA_VERSION
from mailflow.core.models import Attachment, CleanEmail, Direction, Envelope, RawMessage, Recipient


def _recipient(node: dict[str, Any] | None) -> Recipient:
    ea = (node or {}).get("emailAddress", {}) if node else {}
    return Recipient(name=str(ea.get("name", "")), address=str(ea.get("address", "")))


def _recipients(nodes: list[dict[str, Any]] | None) -> list[Recipient]:
    return [_recipient(n) for n in (nodes or []) if (n or {}).get("emailAddress", {}).get("address")]


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _body(data: dict[str, Any]) -> tuple[str, str]:
    body = data.get("body") or {}
    content = str(body.get("content", ""))
    if str(body.get("contentType", "")).lower() == "html":
        return "", content
    return content, ""


def _attachments(data: dict[str, Any]) -> list[Attachment]:
    out: list[Attachment] = []
    for a in data.get("_attachments", []) or []:
        is_inline = bool(a.get("isInline")) or bool(a.get("contentId"))
        out.append(Attachment(
            filename=str(a.get("name", "")),
            content_type=str(a.get("contentType", "application/octet-stream")),
            size_bytes=int(a.get("size", 0) or 0),
            content_id=str(a.get("contentId") or ""),
            is_inline=is_inline,
            provider_attachment_id=str(a.get("id", "")),
        ))
    return out


class GraphExtractor:
    def extract(self, msg: RawMessage, env: Envelope) -> CleanEmail:
        data: dict[str, Any] = json.loads(msg.raw_bytes or b"{}")
        body_text, body_html = _body(data)
        from_ = _recipient(data.get("from"))
        direction = (
            Direction.outbound
            if from_.address.lower() == msg.stream.mailbox.lower()
            else Direction.inbound
        )
        headers = env.headers
        reply_to_list = data.get("replyTo") or []
        references_hdr = headers.get("references")
        ce_kwargs: dict[str, Any] = {
            "canonical_id": env.canonical_id,
            "message_id": env.message_id,
            "message_id_present": env.message_id_present,
            "message_id_trusted": env.message_id_trusted,
            "in_reply_to": _first_or_none(headers.get("in-reply-to")),
            "references": references_hdr[0].split() if references_hdr else [],
            "provider": msg.provider,
            "provider_message_id": msg.provider_message_id,
            "provider_stream_id": msg.stream.key,
            "direction": direction,
            "is_draft": bool(data.get("isDraft", False)),
            "from": from_,
            "sender": _recipient(data.get("sender")) if data.get("sender") else None,
            "reply_to": _recipient(reply_to_list[0]) if reply_to_list else None,
            "to": _recipients(data.get("toRecipients")),
            "cc": _recipients(data.get("ccRecipients")),
            "bcc": _recipients(data.get("bccRecipients")),
            "subject": str(data.get("subject", "")),
            "date_utc": _dt(data.get("sentDateTime")),
            "received_at": _dt(data.get("receivedDateTime")),
            "body_text": body_text,
            "body_html": body_html,
            "attachments": _attachments(data),
            "categories": [str(c) for c in data.get("categories", []) or []],
            "folder": msg.stream.folder or "",
            "list_id": env.list_id,
            "list_unsubscribe": env.list_unsubscribe,
            "auto_submitted": env.auto_submitted,
            "is_auto_submitted": env.is_auto_submitted,
            "is_bounce": env.is_bounce,
            "message_size_bytes": msg.size_bytes,
            "raw_headers": headers,
            "schema_version": SCHEMA_VERSION,
        }
        return CleanEmail.model_validate(ce_kwargs)


def _first_or_none(values: list[str] | None) -> str | None:
    return values[0] if values else None
