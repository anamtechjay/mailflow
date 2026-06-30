"""Graph extractor HTML->text parity (B5): an HTML-only Graph message must still
produce non-empty body_text, the same way the Gmail/MIME extractor does."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.core.models import Cursor, RawMessage, StreamRef

STREAM = StreamRef(mailbox="ops@acme.com")


def _msg(data: dict) -> RawMessage:
    raw = json.dumps(data).encode()
    return RawMessage(
        provider="graph", provider_message_id="m1", stream=STREAM,
        size_bytes=len(raw), received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        cursor=Cursor(value="1", order=1), raw_bytes=raw,
    )


def _extract(data: dict):
    msg = _msg(data)
    env = GraphEnvelopeParser().parse_envelope(msg, "t")
    return GraphExtractor().extract(msg, env)


def test_html_only_body_populates_body_text() -> None:
    ce = _extract({
        "from": {"emailAddress": {"address": "alice@partner.com"}},
        "subject": "HTML only",
        "body": {
            "contentType": "html",
            "content": "<html><body><p>Hello,</p><p>please send a <b>quote</b>.</p></body></html>",
        },
    })
    assert ce.body_html  # html captured
    assert ce.body_text  # non-empty, derived from html (B5 parity)
    assert "please send a quote" in ce.body_text


def test_plain_text_body_is_preserved() -> None:
    ce = _extract({
        "from": {"emailAddress": {"address": "alice@partner.com"}},
        "body": {"contentType": "text", "content": "just text"},
    })
    assert ce.body_text == "just text"
    assert ce.body_html == ""
