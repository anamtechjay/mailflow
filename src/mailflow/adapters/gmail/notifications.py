"""Parse a Gmail Pub/Sub push payload into (email_address, history_id). Gmail's
notification body is JSON `{"emailAddress","historyId"}`; depending on the delivery
layer it may arrive plain or base64-encoded, so we try both. No content is included —
the historyId is a watermark; the provider diffs the mailbox from the stored cursor."""

from __future__ import annotations

import base64
import json
from typing import Any


def _try(raw: bytes) -> tuple[str, int] | None:
    try:
        obj: Any = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    addr = str(obj.get("emailAddress", ""))
    hid = obj.get("historyId")
    if not addr or hid is None:
        return None
    try:
        return addr, int(hid)
    except (ValueError, TypeError):
        return None


def parse_pubsub_message(data: str | bytes) -> tuple[str, int] | None:
    raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
    direct = _try(bytes(raw))
    if direct is not None:
        return direct
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001 - not base64 -> give up
        return None
    return _try(decoded)
