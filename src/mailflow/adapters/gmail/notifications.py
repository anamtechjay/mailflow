"""Parse a Gmail Pub/Sub push payload into (email_address, history_id). Gmail's
notification body is JSON `{"emailAddress","historyId"}`; depending on the delivery
layer it may arrive plain or base64-encoded, so we try both. No content is included —
the historyId is a watermark; the provider diffs the mailbox from the stored cursor."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any, Mapping

from mailflow.core.errors import MailflowError

if TYPE_CHECKING:
    from mailflow.core.ports import WebhookVerifier


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


def parse_pubsub_message(
    data: str | bytes,
    *,
    verifier: "WebhookVerifier | None" = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[str, int] | None:
    raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
    if verifier is not None:
        # A5: prove the push is genuine before trusting it as a wake-signal. A failed
        # verification drops the notification (the cursor-driven sweep still catches mail).
        try:
            verifier.verify(headers=headers or {}, body=bytes(raw))
        except MailflowError:
            return None
    direct = _try(bytes(raw))
    if direct is not None:
        return direct
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001 - not base64 -> give up
        return None
    return _try(decoded)
