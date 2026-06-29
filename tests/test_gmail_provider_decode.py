"""B3 permanent-at-boundary: invalid base64 in the Gmail raw payload is a PermanentError
(poison message, DLQ — never a retry), and a 404/410 on messages.get surfaces as a
PermanentError too (via the A2 typed mapping)."""

from __future__ import annotations

from typing import Any

import pytest

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.provider import _b64url_decode
from mailflow.core.errors import PermanentError


def test_invalid_base64_raises_permanent_error() -> None:
    with pytest.raises(PermanentError):
        _b64url_decode("a")  # 1 data char -> binascii.Error


def test_valid_base64_still_decodes() -> None:
    import base64

    raw = b"hello world"
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert _b64url_decode(encoded) == raw


class _Resp:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.headers: dict[str, str] = {}
        self.content = b""

    def json(self) -> Any:
        return self._body


class _Transport:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp

    def request(self, method: str, url: str, *, headers: Any, json: Any) -> _Resp:
        return self._resp


class _Token:
    def get_token(self) -> str:
        return "tok"


def test_get_message_raw_404_is_permanent() -> None:
    client = GmailClient(
        base_url="https://gmail.googleapis.com/gmail/v1",
        token_provider=_Token(),
        transport=_Transport(_Resp(404, {"error": {"message": "not found"}})),
        max_retries=0,
    )
    with pytest.raises(PermanentError):
        client.get_message_raw("me", "m1")
