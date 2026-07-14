"""Build one logical email spec, then render it as either shape a real provider
would hand the pipeline:

  - ``as_rfc822()``    -> bytes, the Gmail/memory raw-message shape (MimeExtractor).
  - ``as_graph_json()`` -> dict, the Microsoft Graph message shape (GraphExtractor).

Keeping both renderers on one spec means a single `Email(...)` call can seed
both a memory-pipeline test and a Graph-pipeline test with equivalent content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from email.policy import default as default_policy
from typing import Any

DEFAULT_SENDER = "a@partner.com"
DEFAULT_MAILBOX = "me@acme.com"


@dataclass
class Email:
    """One logical email, renderable as RFC822 bytes or a Graph message dict.

    `msg_id` seeds both the synthetic Message-ID (``<{msg_id}@partner.com>``) and
    the Graph `id`/`internetMessageId`. Set `message_id_header=False` /
    `from_header=False` to build the "missing Message-ID" / "missing From"
    tricky-corpus cases; set `html` (with or without `body`) for HTML-only mail.
    `attachment` is `(filename, data, content_type)` for a single-attachment case.
    """

    msg_id: str
    sender: str = DEFAULT_SENDER
    to: str = DEFAULT_MAILBOX
    subject: str = ""
    body: str = ""
    html: str | None = None
    message_id_header: bool = True
    from_header: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)
    attachment: tuple[str, bytes, str] | None = None
    received_at: str = "2026-07-03T10:00:00Z"

    def _message_id(self) -> str:
        return f"<{self.msg_id}@partner.com>"

    def as_rfc822(self) -> bytes:
        msg = EmailMessage(policy=default_policy)
        if self.from_header:
            msg["From"] = self.sender
        msg["To"] = self.to
        msg["Subject"] = self.subject
        if self.message_id_header:
            msg["Message-ID"] = self._message_id()
        for key, value in self.extra_headers.items():
            msg[key] = value

        if self.html is not None and self.body:
            msg.set_content(self.body)
            msg.add_alternative(self.html, subtype="html")
        elif self.html is not None:
            msg.set_content(self.html, subtype="html")
        else:
            msg.set_content(self.body)

        if self.attachment is not None:
            filename, data, content_type = self.attachment
            maintype, _, subtype = content_type.partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)

        return msg.as_bytes()

    def as_graph_json(self) -> dict[str, Any]:
        content = self.html if self.html is not None else self.body
        content_type = "html" if self.html is not None else "text"
        return {
            "id": self.msg_id,
            "internetMessageId": self._message_id() if self.message_id_header else "",
            "from": {"emailAddress": {"name": "", "address": self.sender}} if self.from_header else None,
            "toRecipients": [{"emailAddress": {"address": self.to}}],
            "ccRecipients": [],
            "subject": self.subject,
            "body": {"contentType": content_type, "content": content},
            "bodyPreview": content[:50],
            "receivedDateTime": self.received_at,
            "sentDateTime": self.received_at,
            "isDraft": False,
            "hasAttachments": self.attachment is not None,
            "parentFolderId": "inbox",
            "categories": [],
        }


def raw(msg_id: str, **kw: Any) -> bytes:
    """Shortcut: ``raw("M1", subject="hi")`` == ``Email(msg_id="M1", subject="hi").as_rfc822()``."""
    return Email(msg_id=msg_id, **kw).as_rfc822()
