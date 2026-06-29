"""End-to-end Gmail adapter tests with a FAKE HTTP transport (no Google SDK, no network).

Exercises the REAL chain: parse_pubsub_message -> GmailProvider.fetch ->
GmailClient.history_message_ids + get_message_raw -> Pipeline -> GmailPubSubRuntime,
asserting a CleanEmail is emitted. Covers happy path + edge cases + the reliability paths.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from mailflow.adapters.gmail.bootstrap import sweep_once
from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.provider import GmailProvider
from mailflow.adapters.gmail.runtime import GmailPubSubRuntime
from mailflow.core.models import Cursor, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

MBX = "ops@acme.com"
STREAM = StreamRef(mailbox=MBX, folder=None)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _raw(mid: str, sender: str, subject: str) -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: {sender}\r\n"
            f"To: {MBX}\r\nSubject: {subject}\r\n\r\nbody").encode()


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.headers: dict[str, str] = {}
        self.content = b""

    def json(self) -> Any:
        return self._body


class _Transport:
    """Routes by first matching substring in the URL (order matters)."""
    def __init__(self, routes: list[tuple[str, _Resp]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def request(self, method: str, url: str, *, headers: Any, json: Any) -> _Resp:
        self.calls.append(url)
        for key, resp in self.routes:
            if key in url:
                return resp
        return _Resp(404, {"error": {"message": "no route"}})


class _Token:
    def get_token(self) -> str:
        return "tok"


class _Msg:
    def __init__(self, payload: dict) -> None:
        self.data = json.dumps(payload).encode()
        self.acked = False

    def ack(self) -> None:
        self.acked = True


def _build(routes: list[tuple[str, _Resp]], *, start_cursor: int | None = 100):
    transport = _Transport(routes)
    client = GmailClient(base_url="https://gmail.googleapis.com/gmail/v1",
                         token_provider=_Token(), transport=transport)
    cursor = InMemoryCursorStore()
    if start_cursor is not None:
        cursor.commit_if_ahead("t", STREAM, Cursor(value=str(start_cursor), order=start_cursor))
    provider = GmailProvider(client=client, label_id="INBOX", cursor_store=cursor, tenant="t")
    emit, dlq = MemoryEmitter(), MemoryEmitter()
    pipeline = Pipeline(
        provider=provider, parser=MimeEnvelopeParser(), filters=FilterChain([]),
        extractor=MimeExtractor(), emitter=emit, dlq_emitter=dlq,
        cursor_store=cursor, dedupe_store=InMemoryDedupeStore(), blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="t"),
    )
    runtime = GmailPubSubRuntime(provider=provider, pipeline=pipeline)
    return runtime, emit, dlq, cursor, client, transport


# ---- happy path: notification -> CleanEmail ----

def test_e2e_push_notification_to_clean_email() -> None:
    routes = [
        ("/messages/m1", _Resp(200, {"raw": _b64url(_raw("m1", "a@p.com", "hello")),
                                     "sizeEstimate": 100})),
        ("/history", _Resp(200, {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
                                 "historyId": "200"})),
    ]
    runtime, emit, dlq, cursor, _, _ = _build(routes)
    msg = _Msg({"emailAddress": MBX, "historyId": 200})
    runtime.process_messages([msg])

    assert msg.acked is True                                   # acked after success
    assert len(emit.events) == 1 and not dlq.events
    e = emit.events[0].email
    assert e.subject == "hello" and e.from_.address == "a@p.com"
    got = cursor.get("t", STREAM)
    assert got is not None and got.order == 200                # cursor advanced


# ---- edge: empty history (a non-message change, e.g. "marked read") ----

def test_e2e_empty_history_emits_nothing() -> None:
    routes = [("/history", _Resp(200, {"history": [], "historyId": "200"}))]
    runtime, emit, dlq, cursor, _, _ = _build(routes)
    msg = _Msg({"emailAddress": MBX, "historyId": 200})
    runtime.process_messages([msg])
    assert emit.events == [] and dlq.events == [] and msg.acked is True


# ---- edge: pagination across two history pages ----

def test_e2e_history_pagination_two_pages() -> None:
    routes = [
        ("pageToken=P2", _Resp(200, {"history": [{"messagesAdded": [{"message": {"id": "m2"}}]}],
                                     "historyId": "210"})),
        ("/history", _Resp(200, {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
                                 "nextPageToken": "P2", "historyId": "210"})),
        ("/messages/m1", _Resp(200, {"raw": _b64url(_raw("m1", "a@p.com", "one")), "sizeEstimate": 50})),
        ("/messages/m2", _Resp(200, {"raw": _b64url(_raw("m2", "b@p.com", "two")), "sizeEstimate": 50})),
    ]
    runtime, emit, _, _, _, _ = _build(routes)
    runtime.process_messages([_Msg({"emailAddress": MBX, "historyId": 210})])
    assert {e.email.subject for e in emit.events} == {"one", "two"}


# ---- edge: duplicate message id within history is de-duplicated ----

def test_e2e_duplicate_message_id_in_history_emits_once() -> None:
    routes = [
        ("/history", _Resp(200, {"history": [
            {"messagesAdded": [{"message": {"id": "m1"}}]},
            {"messagesAdded": [{"message": {"id": "m1"}}]},   # same id again
        ], "historyId": "200"})),
        ("/messages/m1", _Resp(200, {"raw": _b64url(_raw("m1", "a@p.com", "dup")), "sizeEstimate": 40})),
    ]
    runtime, emit, _, _, _, _ = _build(routes)
    runtime.process_messages([_Msg({"emailAddress": MBX, "historyId": 200})])
    assert len(emit.events) == 1


# ---- reliability: stale historyId (404) re-seeds the cursor end-to-end ----

def test_e2e_stale_history_404_reseeds_cursor() -> None:
    routes = [
        ("/profile", _Resp(200, {"emailAddress": MBX, "historyId": "500"})),
        ("/history", _Resp(404, {"error": {"message": "historyId too old"}})),
    ]
    runtime, emit, dlq, cursor, _, _ = _build(routes)
    runtime.process_messages([_Msg({"emailAddress": MBX, "historyId": 200})])
    assert emit.events == []                                   # gap skipped
    got = cursor.get("t", STREAM)
    assert got is not None and got.order == 500                # re-seeded to current


# ---- reliability: the sweep catches mail via the stored cursor ----

def test_e2e_sweep_catches_mail() -> None:
    routes = [
        ("/profile", _Resp(200, {"emailAddress": MBX, "historyId": "200"})),
        ("/messages/m9", _Resp(200, {"raw": _b64url(_raw("m9", "c@p.com", "swept")), "sizeEstimate": 30})),
        ("/history", _Resp(200, {"history": [{"messagesAdded": [{"message": {"id": "m9"}}]}],
                                 "historyId": "200"})),
    ]
    runtime, emit, _, _, client, _ = _build(routes)
    sweep_once(runtime=runtime, client=client, mailboxes=[MBX])  # no push — just the sweep
    assert [e.email.subject for e in emit.events] == ["swept"]


# ---- edge: unparseable Pub/Sub message is acked without processing ----

def test_e2e_unparseable_notification_acked_no_emit() -> None:
    runtime, emit, dlq, _, _, _ = _build([("/history", _Resp(200, {"history": [], "historyId": "1"}))])
    bad = _Msg({})  # no emailAddress/historyId
    runtime.process_messages([bad])
    assert bad.acked is True and emit.events == [] and dlq.events == []
