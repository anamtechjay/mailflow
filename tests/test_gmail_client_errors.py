"""A2 typed-raise: GmailClient._request maps HTTP status to the typed error taxonomy
(AuthError/PermanentError/TransientError) before the GmailError fallback, while the
history-404 -> StaleHistoryError special case keeps working (every typed error still
subclasses GmailError so the existing `except GmailError` in history_message_ids is
preserved). Pure fakes, no network."""

from __future__ import annotations

from typing import Any

import pytest

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.transport import GmailError, StaleHistoryError
from mailflow.core.errors import AuthError, PermanentError, TransientError


class _Resp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {"error": {"message": "boom"}}
        self.headers: dict[str, str] = {}
        self.content = b""

    def json(self) -> Any:
        return self._body


class _Transport:
    def __init__(self, resp: _Resp) -> None:
        self._resp = resp
        self.calls: list[str] = []

    def request(self, method: str, url: str, *, headers: Any, json: Any) -> _Resp:
        self.calls.append(url)
        return self._resp


class _Token:
    def get_token(self) -> str:
        return "tok"


def _client(resp: _Resp, *, max_retries: int = 0) -> GmailClient:
    return GmailClient(
        base_url="https://gmail.googleapis.com/gmail/v1",
        token_provider=_Token(),
        transport=_Transport(resp),
        max_retries=max_retries,
    )


@pytest.mark.parametrize(
    "status,exc",
    [
        (401, AuthError),
        (403, PermanentError),
        (404, PermanentError),
        (410, PermanentError),
        (429, TransientError),
        (500, TransientError),
        (503, TransientError),
        (400, GmailError),   # other 4xx -> fallback
        (402, GmailError),
    ],
)
def test_request_maps_status_to_typed_error(status: int, exc: type[Exception]) -> None:
    client = _client(_Resp(status))
    with pytest.raises(exc):
        client.get_profile("me")


def test_typed_errors_still_subclass_gmail_error() -> None:
    # the history-404 special case relies on `except GmailError` catching the typed error.
    client = _client(_Resp(404))
    with pytest.raises(GmailError) as ei:
        client.get_profile("me")
    assert ei.value.status_code == 404


def test_history_404_still_reseeds_via_stale_history_error() -> None:
    client = _client(_Resp(404))
    with pytest.raises(StaleHistoryError):
        client.history_message_ids("me", "100")
