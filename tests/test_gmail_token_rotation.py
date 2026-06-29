"""A8 rotation persistence: when google-auth rotates the OAuth refresh token during a
refresh, OAuthTokenProvider notifies the TokenRotationSink exactly once with the new
token; a refresh that does NOT rotate leaves the sink untouched. Tested with injected
fake credentials so no google-auth dependency is needed."""

from __future__ import annotations

import inspect
from typing import Any

from mailflow.adapters.gmail.live import OAuthTokenProvider, run_service


class _FakeSink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def on_refresh(self, ref: str, new_token: str) -> None:
        self.calls.append((ref, new_token))


class _RotatingCreds:
    """Refresh flips .valid True, sets a token, and rotates the refresh_token."""

    def __init__(self, *, refresh_token: str, new_refresh_token: str | None) -> None:
        self.valid = False
        self.token: str | None = None
        self.refresh_token = refresh_token
        self._new_refresh_token = new_refresh_token

    def refresh(self, request: Any) -> None:
        self.valid = True
        self.token = "access-token"
        if self._new_refresh_token is not None:
            self.refresh_token = self._new_refresh_token


def _provider(creds: _RotatingCreds, sink: _FakeSink) -> OAuthTokenProvider:
    return OAuthTokenProvider(
        client_id="cid", client_secret="csecret", refresh_token=creds.refresh_token,
        token_uri="https://oauth2/token", scopes=["s"],
        rotation_sink=sink, refresh_token_ref="secret/refresh",
        credentials=creds, request_factory=lambda: object(),
    )


def test_rotation_calls_sink_once_with_new_token() -> None:
    creds = _RotatingCreds(refresh_token="old", new_refresh_token="new")
    sink = _FakeSink()
    provider = _provider(creds, sink)
    assert provider.get_token() == "access-token"
    assert sink.calls == [("secret/refresh", "new")]


def test_no_rotation_does_not_call_sink() -> None:
    creds = _RotatingCreds(refresh_token="old", new_refresh_token=None)  # unchanged
    sink = _FakeSink()
    provider = _provider(creds, sink)
    provider.get_token()
    assert sink.calls == []


def test_run_service_exposes_rotation_sink_param() -> None:
    assert "rotation_sink" in inspect.signature(run_service).parameters
