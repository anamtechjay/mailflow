"""B4 reliability: GmailClient retries 429 AND 5xx with bounded exponential backoff +
jitter (honoring Retry-After), and a bounded in-flight concurrency cap that raises
TransientError once saturated. time.sleep is monkeypatched so tests are instant."""

from __future__ import annotations

from typing import Any

import pytest

import mailflow.adapters.gmail.client as client_mod
from mailflow.adapters.gmail.client import GmailClient
from mailflow.core.errors import TransientError


class _Resp:
    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {"ok": True}
        self.headers: dict[str, str] = headers or {}
        self.content = b""

    def json(self) -> Any:
        return self._body


class _SeqTransport:
    """Returns responses from a queue; the last one repeats once exhausted."""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def request(self, method: str, url: str, *, headers: Any, json: Any) -> _Resp:
        self.calls.append(url)
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx]


class _Token:
    def get_token(self) -> str:
        return "tok"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: slept.append(s))
    return slept


def _client(transport: _SeqTransport, *, max_retries: int = 3, max_in_flight: int = 8) -> GmailClient:
    return GmailClient(
        base_url="https://gmail.googleapis.com/gmail/v1",
        token_provider=_Token(),
        transport=transport,
        max_retries=max_retries,
        max_in_flight=max_in_flight,
    )


def test_429_storm_then_success() -> None:
    t = _SeqTransport([_Resp(429), _Resp(429), _Resp(200, {"emailAddress": "x"})])
    client = _client(t, max_retries=3)
    result = client.get_profile("me")
    assert result == {"emailAddress": "x"}
    assert len(t.calls) == 3  # two retries then success


def test_5xx_storm_then_success() -> None:
    t = _SeqTransport([_Resp(503), _Resp(500), _Resp(200, {"emailAddress": "y"})])
    client = _client(t, max_retries=3)
    assert client.get_profile("me") == {"emailAddress": "y"}


def test_persistent_5xx_raises_transient_after_bounded_attempts() -> None:
    t = _SeqTransport([_Resp(500)])
    client = _client(t, max_retries=3)
    with pytest.raises(TransientError):
        client.get_profile("me")
    # bounded: initial attempt + max_retries retries == 4 total requests, no more.
    assert len(t.calls) == 4


def test_retry_after_header_is_honored(_no_sleep: list[float]) -> None:
    t = _SeqTransport([_Resp(429, headers={"Retry-After": "5"}), _Resp(200, {"ok": True})])
    client = _client(t, max_retries=3)
    client.get_profile("me")
    assert _no_sleep and _no_sleep[0] >= 5.0  # waited at least the server-requested delay


def test_concurrency_cap_raises_transient_when_saturated() -> None:
    t = _SeqTransport([_Resp(200, {"ok": True})])
    client = _client(t, max_in_flight=1)
    # saturate the in-flight cap, then a request must fail fast with TransientError.
    assert client._inflight.acquire(blocking=False) is True
    try:
        with pytest.raises(TransientError):
            client.get_profile("me")
    finally:
        client._inflight.release()
    # once released, requests succeed again.
    assert client.get_profile("me") == {"ok": True}
