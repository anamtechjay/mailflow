"""Graph envelope parser populates the is_auto_submitted / is_bounce seam."""

from __future__ import annotations

import json
from datetime import datetime, timezone

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


def test_graph_envelope_auto_submitted_and_bounce() -> None:
    data = {
        "from": {"emailAddress": {"address": "mailer-daemon@mx.example"}},
        "internetMessageHeaders": [
            {"name": "Auto-Submitted", "value": "auto-replied"},
        ],
    }
    env = GraphEnvelopeParser().parse_envelope(_msg(data), "t")
    assert env.is_auto_submitted is True
    assert env.is_bounce is True   # daemon sender


def test_graph_envelope_ordinary_mail_not_classified() -> None:
    data = {"from": {"emailAddress": {"address": "alice@partner.com"}}}
    env = GraphEnvelopeParser().parse_envelope(_msg(data), "t")
    assert env.is_auto_submitted is False
    assert env.is_bounce is False


def test_graph_extractor_copies_seam_from_envelope() -> None:
    from mailflow.adapters.graph.extractor import GraphExtractor
    data = {
        "from": {"emailAddress": {"address": "mailer-daemon@mx.example"}},
        "internetMessageHeaders": [{"name": "Auto-Submitted", "value": "auto-replied"}],
    }
    msg = _msg(data)
    env = GraphEnvelopeParser().parse_envelope(msg, "t")
    ce = GraphExtractor().extract(msg, env)
    assert ce.is_auto_submitted is True
    assert ce.is_bounce is True
