"""F17 — Fetch-by-id (integration). See docs/qa-partA-coverage.md.

`Mailflow.get_email` / `get_body` / `get_recipients` / `get_attachments` for a live
provider (Graph), and the documented `NotImplementedError` for the memory provider
(nothing to fetch -- memory is a finite seed, not a live mailbox).

`connect("graph", ...)` builds its fetcher lazily from `credentials` (real MSAL token
+ HTTP client, see facade.py `_build_graph_fetcher`), so it can't be driven with a fake
transport without network. Instead this test replicates that closure's logic --
GraphClient (fake transport + fake token) -> GraphEnvelopeParser -> GraphExtractor --
and wires the result into a `Mailflow` handle directly, exercising the exact same
fetch-by-id code path facade.py uses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.core.models import CleanEmail, Cursor, RawMessage, StreamRef
from mailflow.emit.memory import MemoryEmitter
from mailflow.facade import Mailflow, connect
from mailflow.stores.memory import InMemoryCursorStore

from tests._harness.fakes import _FakeGraphTransport, _graph_message

TENANT = "acme"
MBX = "ops@acme.com"


class _FakeToken:
    def get_token(self) -> str:
        return "tok"


def _build_fetcher(
    messages: dict[str, dict[str, Any]],
    attachments: dict[str, list[dict[str, Any]]] | None = None,
) -> Any:
    """Replicates facade.py's `_build_graph_fetcher` inner `fetch()` closure, wired to
    the fake Graph transport instead of a real HTTP client + MSAL token."""
    client = GraphClient(
        base_url="https://graph.microsoft.com/v1.0",
        token_provider=_FakeToken(),
        transport=_FakeGraphTransport(messages, attachments=attachments),
    )
    parser = GraphEnvelopeParser()
    extractor = GraphExtractor()

    def fetch(message_id: str) -> CleanEmail:
        data: dict[str, Any] = client.get_message(MBX, message_id)
        data["_attachments"] = (
            client.list_attachments(MBX, message_id) if data.get("hasAttachments") else []
        )
        raw = json.dumps(data).encode()
        stream = StreamRef(mailbox=MBX, folder=str(data.get("parentFolderId", "") or ""))
        msg = RawMessage(
            provider="graph", provider_message_id=message_id, stream=stream,
            size_bytes=len(raw), received_at=datetime.now(timezone.utc),
            cursor=Cursor(value="fetch", order=0), raw_bytes=raw,
        )
        env = parser.parse_envelope(msg, tenant=TENANT)
        return extractor.extract(msg, env)

    return fetch


def _graph_handle(fetch: Any) -> Mailflow:
    return Mailflow(
        provider_kind="graph", emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), fetcher=fetch,
    )


def test_memory_get_email_not_implemented() -> None:
    mf = connect("memory", seed={})
    with pytest.raises(NotImplementedError):
        mf.get_email("x")


def test_get_email_maps_fields() -> None:
    msg = _graph_message(
        "MSG1", mailbox=MBX, sender="alice@partner.com", subject="Invoice #42", body="hello body",
    )
    mf = _graph_handle(_build_fetcher({"MSG1": msg}))

    email = mf.get_email("MSG1")

    assert isinstance(email, CleanEmail)
    assert email.subject == "Invoice #42"
    assert email.from_.address == "alice@partner.com"
    assert [r.address for r in email.to] == [MBX]
    assert email.provider == "graph"
    assert email.provider_message_id == "MSG1"
    assert email.canonical_id == "<MSG1@partner.com>"  # trusted internetMessageId, used verbatim
    assert email.body_text == "hello body"


def test_get_body_recipients_attachments() -> None:
    msg = _graph_message("AT1", mailbox=MBX, has_attachments=True, body="body text here")
    atts = {
        "AT1": [{
            "id": "a1", "name": "invoice.pdf", "contentType": "application/pdf",
            "size": 1234, "isInline": False,
        }],
    }
    mf = _graph_handle(_build_fetcher({"AT1": msg}, attachments=atts))

    assert mf.get_body("AT1") == "body text here"

    recipients = mf.get_recipients("AT1")
    assert [r.address for r in recipients] == [MBX]

    fetched_attachments = mf.get_attachments("AT1")
    assert len(fetched_attachments) == 1
    assert fetched_attachments[0].filename == "invoice.pdf"
    assert fetched_attachments[0].content_type == "application/pdf"
    assert fetched_attachments[0].size_bytes == 1234
