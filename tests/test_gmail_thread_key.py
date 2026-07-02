"""A7 (adapter part): GmailProvider.fetch carries the Gmail threadId from the
messages.get resource onto RawMessage.thread_key (the extractor seam then threads it
onto CleanEmail)."""

from __future__ import annotations

import base64
from typing import Any

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.provider import GmailProvider
from mailflow.core.models import Cursor, StreamRef

STREAM = StreamRef(mailbox="ops@acme.com", folder=None)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _raw(mid: str) -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: a@p.com\r\n"
            f"To: ops@acme.com\r\nSubject: {mid}\r\n\r\nbody").encode()


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.headers: dict[str, str] = {}
        self.content = b""

    def json(self) -> Any:
        return self._body


class _Transport:
    def __init__(self, routes: list[tuple[str, _Resp]]) -> None:
        self.routes = routes

    def request(self, method: str, url: str, *, headers: Any, json: Any) -> _Resp:
        for key, resp in self.routes:
            if key in url:
                return resp
        return _Resp(404, {"error": {"message": "no route"}})


class _Token:
    def get_token(self) -> str:
        return "tok"


def _provider(routes: list[tuple[str, _Resp]]) -> GmailProvider:
    client = GmailClient(
        base_url="https://gmail.googleapis.com/gmail/v1",
        token_provider=_Token(), transport=_Transport(routes), max_retries=0,
    )
    return GmailProvider(client=client, label_id="INBOX", tenant="t")


def test_fetch_carries_thread_id_as_thread_key() -> None:
    routes = [
        ("/history", _Resp(200, {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
                                 "historyId": "200"})),
        ("/messages/m1", _Resp(200, {"raw": _b64url(_raw("m1")), "threadId": "T-42",
                                     "sizeEstimate": 50})),
    ]
    out = list(_provider(routes).fetch(STREAM, Cursor(value="100", order=100)))
    assert len(out) == 1
    assert out[0].thread_key == "T-42"


def test_fetch_missing_thread_id_defaults_empty() -> None:
    routes = [
        ("/history", _Resp(200, {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
                                 "historyId": "200"})),
        ("/messages/m1", _Resp(200, {"raw": _b64url(_raw("m1")), "sizeEstimate": 50})),
    ]
    out = list(_provider(routes).fetch(STREAM, Cursor(value="100", order=100)))
    assert out[0].thread_key == ""
