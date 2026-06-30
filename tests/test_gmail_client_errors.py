"""A2 typed-raise: GmailClient._request maps HTTP status to the typed error taxonomy
(AuthError/PermanentError/TransientError) before the GmailError fallback, while the
history-404 -> StaleHistoryError special case keeps working (every typed error still
subclasses GmailError so the existing `except GmailError` in history_message_ids is
preserved). Pure fakes, no network."""

from __future__ import annotations

from typing import Any

import pytest

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.transport import GmailAuthError, GmailError, StaleHistoryError
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


# A2: adapter-local refresh-once on a fetch-time 401. The pipeline-level AuthError handler
# never sees a 401 raised inside provider.fetch(), so the HTTP client refreshes the token
# and retries the request exactly once; a second 401 propagates as GmailAuthError.
class _SeqTransport:
    def __init__(self, statuses: list[int]) -> None:
        self._statuses = list(statuses)
        self.auth_headers: list[str] = []

    def request(self, method: str, url: str, *, headers: dict[str, str], json: Any) -> _Resp:
        self.auth_headers.append(headers["Authorization"])
        return _Resp(self._statuses.pop(0), {"ok": True})


class _RefreshableTokens:
    def __init__(self) -> None:
        self.token = "t0"
        self.refreshes = 0

    def get_token(self) -> str:
        return self.token

    def force_refresh(self) -> None:
        self.refreshes += 1
        self.token = f"t{self.refreshes}"


def test_client_refreshes_and_retries_once_on_401() -> None:
    tokens = _RefreshableTokens()
    transport = _SeqTransport([401, 200])
    client = GmailClient(
        base_url="https://g", token_provider=tokens, transport=transport, max_retries=0,
    )
    client.get_profile("me@x")
    assert tokens.refreshes == 1
    assert transport.auth_headers == ["Bearer t0", "Bearer t1"]  # retried with fresh token


def test_client_raises_auth_error_after_second_401_no_loop() -> None:
    tokens = _RefreshableTokens()
    client = GmailClient(
        base_url="https://g", token_provider=tokens,
        transport=_SeqTransport([401, 401]), max_retries=0,
    )
    with pytest.raises(GmailAuthError):
        client.get_profile("me@x")
    assert tokens.refreshes == 1  # exactly one refresh, no loop
