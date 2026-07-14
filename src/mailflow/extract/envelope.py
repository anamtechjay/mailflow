"""RawMessage -> Envelope (spec §7.3): headers + a snippet, no body decode."""

from __future__ import annotations

from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses
from typing import Any

from mailflow.core.classification import derive_auto_submitted, derive_is_bounce
from mailflow.core.identity import derive_canonical_id
from mailflow.core.models import Envelope, RawMessage, Recipient


class MimeEnvelopeParser:
    def __init__(self, snippet_chars: int = 256) -> None:
        self.snippet_chars = snippet_chars

    def parse_envelope(self, msg: RawMessage, tenant: str) -> Envelope:
        parsed = message_from_bytes(msg.raw_bytes, policy=default_policy)
        assert isinstance(parsed, EmailMessage)

        def recips(header: str) -> list[Recipient]:
            return [
                Recipient(name=n, address=a)
                for n, a in getaddresses(parsed.get_all(header, []))
                if a
            ]

        message_id_raw = parsed["message-id"]
        message_id = str(message_id_raw) if message_id_raw is not None else None
        canonical_id, present, trusted = derive_canonical_id(
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            mailbox=msg.stream.mailbox,
            message_id=message_id,
        )

        from_ = recips("from")
        snippet = self._snippet(parsed)
        headers: dict[str, list[str]] = {}
        for k, v in parsed.items():
            headers.setdefault(k.lower(), []).append(str(v))

        senders = recips("sender")
        reply_tos = recips("reply-to")
        alias_from: dict[str, Any] = {"from": from_[0] if from_ else Recipient()}

        auto_sub = str(parsed["auto-submitted"]) if parsed["auto-submitted"] else None
        content_type = str(parsed["content-type"]) if parsed["content-type"] is not None else None
        return_path = str(parsed["return-path"]) if parsed["return-path"] is not None else None
        from_addr = from_[0].address if from_ else ""

        return Envelope(
            canonical_id=canonical_id,
            message_id=message_id,
            message_id_present=present,
            message_id_trusted=trusted,
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            stream=msg.stream,
            **alias_from,
            sender=senders[0] if senders else None,
            reply_to=reply_tos[0] if reply_tos else None,
            to=recips("to"),
            cc=recips("cc"),
            subject=str(parsed["subject"] or ""),
            received_at=msg.received_at,
            snippet=snippet,
            list_id=str(parsed["list-id"]) if parsed["list-id"] else None,
            list_unsubscribe=str(parsed["list-unsubscribe"]) if parsed["list-unsubscribe"] else None,
            auto_submitted=auto_sub,
            is_auto_submitted=derive_auto_submitted(auto_sub),
            is_bounce=derive_is_bounce(
                from_address=from_addr, return_path=return_path, content_type=content_type
            ),
            headers=headers,
        )

    def _snippet(self, parsed: EmailMessage) -> str:
        body = parsed.get_body(preferencelist=("plain", "html"))
        if body is None:
            return ""
        # MIME-2: an unknown/bogus declared charset makes get_content() raise
        # LookupError (and malformed bytes raise UnicodeError). Fall back to a
        # permissive UTF-8-with-replacement decode of the transfer-decoded payload so
        # a foreign-charset message is not lost at the (pre-extraction) envelope stage.
        try:
            text = str(body.get_content())
        except (LookupError, UnicodeError):
            payload = body.get_payload(decode=True)
            text = (
                bytes(payload).decode("utf-8", errors="replace")
                if isinstance(payload, (bytes, bytearray))
                else str(body.get_payload())
            )
        return text.strip().replace("\r\n", " ").replace("\n", " ")[: self.snippet_chars]
