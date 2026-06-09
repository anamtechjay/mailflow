"""Convert real Gmail corpus records into RFC822 bytes for the MemoryProvider.

Independent of the mailflow extractor: we build the message from the convenience
fields, the extractor parses it back. Agreement is the golden test."""

from __future__ import annotations

import json
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import format_datetime
from datetime import datetime
from pathlib import Path

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

FIXTURE_DIR = Path(__file__).parent / "cowboy_thread"
WATCHED_MAILBOX = "testuser1@cowboyslogistics.com"
STREAM = StreamRef(mailbox=WATCHED_MAILBOX, folder="Inbox")

# Real providers deliver Message-IDs unfolded on a single line. The default
# email policy folds at 78 cols, which would split the long gmail Message-IDs
# (`<CAM0jTA...@mail.gmail.com>`, ~68 chars + header name) onto a continuation
# line; re-parsing then prepends a leading space to the value — a pure RFC822
# round-trip artifact, not how the wire bytes actually arrive. Serializing at
# the RFC 5322 hard line limit (998) keeps these headers on one line so the
# reconstructed bytes faithfully mirror a provider's delivery.
_NO_FOLD_POLICY = default_policy.clone(max_line_length=998)


def load_corpus() -> list[dict]:
    return json.loads((FIXTURE_DIR / "corpus.json").read_text())


def load_golden() -> dict[str, dict]:
    return json.loads((FIXTURE_DIR / "golden_cleanemails.json").read_text())


def record_to_rfc822(rec: dict) -> bytes:
    msg = EmailMessage(policy=_NO_FOLD_POLICY)
    msg["Message-ID"] = rec["message_id_header"]
    msg["From"] = rec["from_email"]
    msg["To"] = rec["to_email"]
    if rec["cc_emails"]:
        msg["Cc"] = ", ".join(rec["cc_emails"])
    msg["Subject"] = rec["subject"]
    if rec["in_reply_to"]:
        msg["In-Reply-To"] = rec["in_reply_to"]
    if rec["references"]:
        msg["References"] = " ".join(rec["references"])
    try:
        msg["Date"] = format_datetime(datetime.fromisoformat(rec["date"]))
    except (TypeError, ValueError):
        pass
    msg.set_content(rec["body"])  # UTF-8; bodies contain → and curly quotes
    return msg.as_bytes()


def seed() -> dict[StreamRef, list[SeedEmail]]:
    emails = [
        SeedEmail(provider_message_id=rec["provider_message_id"], raw=record_to_rfc822(rec))
        for rec in load_corpus()
    ]
    return {STREAM: emails}
