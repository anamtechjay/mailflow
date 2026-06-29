"""B2 fetch-stage isolation: a single poison record (bad base64 / PermanentError on
messages.get) is skipped so the rest of the batch still flows; an AuthError or
TransientError mid-batch propagates (it's a whole-stream problem, not one record)."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.provider import GmailProvider
from mailflow.core.errors import AuthError, TransientError
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


def test_poison_record_skipped_good_ones_yielded() -> None:
    routes = [
        ("/history", _Resp(200, {"history": [
            {"messagesAdded": [{"message": {"id": "m1"}}]},
            {"messagesAdded": [{"message": {"id": "m2"}}]},  # poison: bad base64
            {"messagesAdded": [{"message": {"id": "m3"}}]},
        ], "historyId": "200"})),
        ("/messages/m1", _Resp(200, {"raw": _b64url(_raw("m1")), "sizeEstimate": 50})),
        ("/messages/m2", _Resp(200, {"raw": "a", "sizeEstimate": 50})),  # invalid base64
        ("/messages/m3", _Resp(200, {"raw": _b64url(_raw("m3")), "sizeEstimate": 50})),
    ]
    provider = _provider(routes)
    out = list(provider.fetch(STREAM, Cursor(value="100", order=100)))
    assert [m.provider_message_id for m in out] == ["m1", "m3"]


def test_permanent_404_on_fetch_skips_record() -> None:
    routes = [
        ("/history", _Resp(200, {"history": [
            {"messagesAdded": [{"message": {"id": "m1"}}]},
            {"messagesAdded": [{"message": {"id": "mX"}}]},  # 404 on get -> PermanentError
        ], "historyId": "200"})),
        ("/messages/m1", _Resp(200, {"raw": _b64url(_raw("m1")), "sizeEstimate": 50})),
        ("/messages/mX", _Resp(404, {"error": {"message": "gone"}})),
    ]
    provider = _provider(routes)
    out = list(provider.fetch(STREAM, Cursor(value="100", order=100)))
    assert [m.provider_message_id for m in out] == ["m1"]


def test_transient_5xx_on_fetch_propagates() -> None:
    routes = [
        ("/history", _Resp(200, {"history": [
            {"messagesAdded": [{"message": {"id": "m1"}}]},
        ], "historyId": "200"})),
        ("/messages/m1", _Resp(503, {"error": {"message": "unavailable"}})),
    ]
    provider = _provider(routes)
    with pytest.raises(TransientError):
        list(provider.fetch(STREAM, Cursor(value="100", order=100)))


def test_auth_401_on_fetch_propagates() -> None:
    routes = [
        ("/history", _Resp(200, {"history": [
            {"messagesAdded": [{"message": {"id": "m1"}}]},
        ], "historyId": "200"})),
        ("/messages/m1", _Resp(401, {"error": {"message": "invalid creds"}})),
    ]
    provider = _provider(routes)
    with pytest.raises(AuthError):
        list(provider.fetch(STREAM, Cursor(value="100", order=100)))
