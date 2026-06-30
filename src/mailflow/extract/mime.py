"""RFC822 bytes -> CleanEmail (spec §7.6). Uses stdlib `email` with the modern
policy so headers come back parsed and unfolded."""

from __future__ import annotations

from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from mailflow.core.events import SCHEMA_VERSION
from mailflow.core.identity import derive_canonical_id
from mailflow.core.models import Attachment, CleanEmail, Direction, Recipient, ScanVerdict
from mailflow.core.ports import AttachmentScanner, BlobStore
from mailflow.extract.clean import html_to_text, normalize_subject
from mailflow.extract.safety import (
    DEFAULT_ALLOWLIST,
    AttachmentBlockedError,
    NoOpAttachmentScanner,
    check_allowlist,
)
from mailflow.extract.streaming import (
    MAX_ATTACHMENT_BYTES,
    digest_and_size,
    iter_decoded,
)


def _recipients(msg: EmailMessage, header: str) -> list[Recipient]:
    values = msg.get_all(header, [])
    return [Recipient(name=name, address=addr) for name, addr in getaddresses(values) if addr]


def _one(msg: EmailMessage, header: str) -> Recipient | None:
    rs = _recipients(msg, header)
    return rs[0] if rs else None


def _raw_headers(msg: EmailMessage) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in msg.items():
        out.setdefault(key.lower(), []).append(str(value))
    return out


class MimeExtractor:
    """ContentExtractor implementation for raw RFC822 (Gmail format=raw / memory)."""

    def __init__(
        self,
        *,
        scanner: AttachmentScanner | None = None,
        allowlist: frozenset[str] = DEFAULT_ALLOWLIST,
        max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
    ) -> None:
        self.scanner: AttachmentScanner = scanner or NoOpAttachmentScanner()
        self.allowlist = allowlist
        self.max_attachment_bytes = max_attachment_bytes

    def extract_bytes(
        self,
        raw: bytes,
        *,
        provider: str,
        provider_message_id: str,
        stream_id: str,
        watched_mailbox: str,
        blob_store: BlobStore | None = None,
        thread_key: str = "",
    ) -> CleanEmail:
        msg = message_from_bytes(raw, policy=default_policy)
        assert isinstance(msg, EmailMessage)

        message_id_raw = msg["message-id"]
        message_id = str(message_id_raw) if message_id_raw is not None else None
        mailbox = watched_mailbox
        canonical_id, present, trusted = derive_canonical_id(
            provider=provider,
            provider_message_id=provider_message_id,
            mailbox=mailbox,
            message_id=message_id,
        )

        from_ = _one(msg, "from") or Recipient()
        direction = (
            Direction.outbound
            if from_.address.lower() == watched_mailbox.lower()
            else Direction.inbound
        )

        body_text, body_html, attachments = self._walk_body(msg, blob_store)

        date_hdr = msg["date"]
        date_utc = None
        if date_hdr is not None:
            try:
                date_utc = parsedate_to_datetime(str(date_hdr))
            except (TypeError, ValueError):
                date_utc = None

        refs = msg.get_all("references", [])
        references = " ".join(str(r) for r in refs).split() if refs else []

        alias_from: dict[str, Any] = {"from": from_}

        subject = str(msg["subject"] or "")
        # A7 subject-fallback: when the adapter passes no provider thread id, group
        # by a case-insensitive Re:/Fwd:-stripped subject so a reply joins its root.
        effective_thread_key = thread_key or normalize_subject(subject).lower()

        return CleanEmail(
            canonical_id=canonical_id,
            message_id=message_id,
            message_id_present=present,
            message_id_trusted=trusted,
            in_reply_to=str(msg["in-reply-to"]) if msg["in-reply-to"] else None,
            references=references,
            provider=provider,
            provider_message_id=provider_message_id,
            provider_stream_id=stream_id,
            thread_key=effective_thread_key,
            direction=direction,
            **alias_from,  # alias
            sender=_one(msg, "sender"),
            reply_to=_one(msg, "reply-to"),
            to=_recipients(msg, "to"),
            cc=_recipients(msg, "cc"),
            bcc=_recipients(msg, "bcc"),
            subject=subject,
            date_utc=date_utc,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            auto_submitted=str(msg["auto-submitted"]) if msg["auto-submitted"] else None,
            list_id=str(msg["list-id"]) if msg["list-id"] else None,
            list_unsubscribe=str(msg["list-unsubscribe"]) if msg["list-unsubscribe"] else None,
            message_size_bytes=len(raw),
            raw_headers=_raw_headers(msg),
            schema_version=SCHEMA_VERSION,
        )

    def _walk_body(
        self, msg: EmailMessage, blob_store: BlobStore | None = None
    ) -> tuple[str, str, list[Attachment]]:
        body_text = ""
        body_html = ""
        attachments: list[Attachment] = []

        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = (part.get_content_disposition() or "").lower()
            cid = part.get("content-id")
            filename = part.get_filename()

            is_attachment = disp == "attachment" or (bool(filename) and disp != "inline")
            is_inline_media = disp == "inline" or cid is not None

            if not is_attachment and not is_inline_media and ctype == "text/plain" and not body_text:
                body_text = part.get_content()
                continue
            if not is_attachment and not is_inline_media and ctype == "text/html" and not body_html:
                body_html = part.get_content()
                continue

            if is_attachment or is_inline_media:
                content_hash, size_bytes = digest_and_size(
                    part, cap=self.max_attachment_bytes
                )
                meta = Attachment(
                    filename=filename or "",
                    content_type=ctype,
                    size_bytes=size_bytes,
                    content_hash=content_hash,
                    content_id=str(cid) if cid else "",
                    is_inline=bool(is_inline_media and not is_attachment),
                )
                # Safety seam: allowlist first, then the scanner hook. Runs BEFORE any
                # blob is persisted; a block fails closed -> DLQ. NOTE: this block sits
                # under `is_attachment or is_inline_media`, so it intentionally governs
                # inline media (logos, tracking pixels) too — one blocked part DLQs the
                # whole message (a non-empty allowlist must include expected inline types).
                result = check_allowlist(meta, self.allowlist)
                if result.verdict is ScanVerdict.allow:
                    result = self.scanner.scan(meta)
                if result.verdict is ScanVerdict.block:
                    raise AttachmentBlockedError(meta.filename, result.reason)
                storage_ref = ""
                # Stream the decoded bytes into the blob store in chunks (avoids a
                # second full copy of the decoded payload) so downstream apps can
                # download the file (else metadata only).
                if blob_store is not None and size_bytes:
                    storage_ref = blob_store.put_stream(
                        content_hash, iter_decoded(part), ctype
                    )
                attachments.append(meta.model_copy(update={"storage_ref": storage_ref}))

        # Gentle HTML->text fallback when the part set is HTML-only (B5/A6).
        if not body_text and body_html:
            body_text = html_to_text(body_html)

        return body_text, body_html, attachments
