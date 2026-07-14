"""Graph threading — the extractor must map Graph's `conversationId` onto
`CleanEmail.thread_key`, matching how the Gmail provider maps `threadId` (A7).
Without this, every Graph email lands with thread_key="" and conversation
grouping on the poll/Event-Hubs path silently doesn't work."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.core.models import Cursor, RawMessage, StreamRef

from tests._harness.fakes import _graph_message

MBX = "ops@acme.com"


def _raw(data: dict) -> RawMessage:
    rb = json.dumps(data).encode("utf-8")
    return RawMessage(
        provider="graph", provider_message_id=str(data["id"]),
        stream=StreamRef(mailbox=MBX, folder="inbox"),
        size_bytes=len(rb), received_at=datetime.now(timezone.utc),
        cursor=Cursor(value="c1", order=1), raw_bytes=rb,
    )


def test_extractor_maps_conversation_id_to_thread_key() -> None:
    data = _graph_message("M1", mailbox=MBX, conversation_id="CONV-123")
    msg = _raw(data)
    clean = GraphExtractor().extract(msg, GraphEnvelopeParser().parse_envelope(msg, tenant="acme"))
    assert clean.thread_key == "CONV-123"


def test_two_messages_same_conversation_share_thread_key() -> None:
    ext, parser = GraphExtractor(), GraphEnvelopeParser()
    a = _raw(_graph_message("A", mailbox=MBX, conversation_id="T-9"))
    b = _raw(_graph_message("B", mailbox=MBX, conversation_id="T-9"))
    ca = ext.extract(a, parser.parse_envelope(a, tenant="acme"))
    cb = ext.extract(b, parser.parse_envelope(b, tenant="acme"))
    assert ca.thread_key == cb.thread_key == "T-9"


def test_missing_conversation_id_yields_empty_thread_key() -> None:
    data = _graph_message("M2", mailbox=MBX)  # no conversation_id
    msg = _raw(data)
    clean = GraphExtractor().extract(msg, GraphEnvelopeParser().parse_envelope(msg, tenant="acme"))
    assert clean.thread_key == ""
