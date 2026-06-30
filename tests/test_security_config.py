"""Task 3: read_allowlist is deleted (was unenforced, fake fail-closed); the
verify_scope_on_startup knob remains and is real."""

from __future__ import annotations

from mailflow.config.schema import SecurityConfig


def test_read_allowlist_field_removed() -> None:
    assert "read_allowlist" not in SecurityConfig.model_fields


def test_verify_scope_on_startup_default_true() -> None:
    assert "verify_scope_on_startup" in SecurityConfig.model_fields
    assert SecurityConfig().verify_scope_on_startup is True


import pytest

from mailflow.adapters.gmail.config import GMAIL_READONLY
from mailflow.adapters.gmail.live import OAuthTokenProvider
from mailflow.core.errors import AuthError


class _FakeCreds:
    """Stand-in for google.oauth2.credentials.Credentials with the granted-scope
    surface google-auth populates after refresh()."""

    def __init__(self, *, granted: str | None, valid: bool = False) -> None:
        self.token = "access-token"
        self.refresh_token = "rt"
        self.valid = valid
        self.granted_scopes = granted

    def refresh(self, request: object) -> None:
        self.valid = True


def _provider(creds: _FakeCreds) -> OAuthTokenProvider:
    return OAuthTokenProvider(
        client_id="cid", client_secret="sec", refresh_token="rt",
        token_uri="https://oauth2.googleapis.com/token", scopes=[GMAIL_READONLY],
        credentials=creds, request_factory=lambda: object(),
    )


def test_verify_scopes_passes_when_granted() -> None:
    _provider(_FakeCreds(granted=GMAIL_READONLY)).verify_scopes([GMAIL_READONLY])


def test_verify_scopes_raises_when_missing() -> None:
    prov = _provider(_FakeCreds(granted="https://www.googleapis.com/auth/userinfo.email"))
    with pytest.raises(AuthError) as exc:
        prov.verify_scopes([GMAIL_READONLY])
    assert GMAIL_READONLY in str(exc.value)


from mailflow.adapters.gmail.live import verify_oauth_scopes


class _SpyTokenProvider:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def verify_scopes(self, required: list[str]) -> None:
        self.calls.append(required)


def test_verify_oauth_scopes_runs_when_enabled() -> None:
    spy = _SpyTokenProvider()
    verify_oauth_scopes(spy, [GMAIL_READONLY], enabled=True)
    assert spy.calls == [[GMAIL_READONLY]]


def test_verify_oauth_scopes_skipped_when_disabled() -> None:
    spy = _SpyTokenProvider()
    verify_oauth_scopes(spy, [GMAIL_READONLY], enabled=False)
    assert spy.calls == []


import inspect

from mailflow.facade import connect


def test_connect_exposes_verify_scope_param_defaulted_from_schema() -> None:
    param = inspect.signature(connect).parameters["verify_scope_on_startup"]
    assert param.default is SecurityConfig().verify_scope_on_startup is True
