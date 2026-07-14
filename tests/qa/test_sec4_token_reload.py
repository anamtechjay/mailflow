"""SEC-4 — on startup the runtime must prefer a PERSISTED (rotated) refresh token over
the stale configured one. Google rotates the OAuth refresh token; a prior run saves the
new token via `TokenRotationSink.on_refresh`, but the env/config `oauth_refresh_token_ref`
still holds the OLD token. If the next run reads only the configured value, auth fails.
`resolve_refresh_token` loads the persisted token when the sink can supply one.
"""

from __future__ import annotations

from mailflow.adapters.gmail.live import resolve_refresh_token


class _Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get(self, ref: str) -> str:
        return self._value


class _Sink:
    """A rotation sink that persisted a rotated token (implements load)."""

    def __init__(self, persisted: str | None) -> None:
        self._persisted = persisted

    def on_refresh(self, ref: str, new_token: str) -> None: ...

    def load(self, ref: str) -> str | None:
        return self._persisted


class _LoadlessSink:
    """A custom consumer sink that only implements the port's on_refresh."""

    def on_refresh(self, ref: str, new_token: str) -> None: ...


def test_persisted_token_is_preferred_over_configured():
    tok = resolve_refresh_token(
        secret_provider=_Secret("OLD"), ref="env://RT", rotation_sink=_Sink("NEW")
    )
    assert tok == "NEW"


def test_configured_used_when_nothing_persisted():
    tok = resolve_refresh_token(
        secret_provider=_Secret("OLD"), ref="env://RT", rotation_sink=_Sink(None)
    )
    assert tok == "OLD"


def test_configured_used_when_no_sink():
    tok = resolve_refresh_token(
        secret_provider=_Secret("OLD"), ref="env://RT", rotation_sink=None
    )
    assert tok == "OLD"


def test_loadless_sink_falls_back_to_configured():
    tok = resolve_refresh_token(
        secret_provider=_Secret("OLD"), ref="env://RT", rotation_sink=_LoadlessSink()
    )
    assert tok == "OLD"
