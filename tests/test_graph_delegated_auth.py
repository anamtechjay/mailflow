"""Delegated Microsoft Graph OAuth — unit tests for the pure/testable pieces (URL and
request shaping, and the refresh-token access-token provider with an injected poster).
The interactive browser+local-server flow itself is not unit-tested (same as the Gmail
InstalledAppFlow), only the deterministic parts around it."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

import base64
import hashlib

from mailflow.adapters.graph.delegated_auth import (
    DelegatedGraphTokenProvider,
    build_authorize_url,
    generate_pkce_pair,
    refresh_request_body,
    token_request_body,
)

TENANT = "contoso.onmicrosoft.com"
CLIENT = "app-guid"
REDIRECT = "http://localhost:8765/"


def test_authorize_url_targets_tenant_and_requests_offline_access():
    url = build_authorize_url(
        tenant=TENANT, client_id=CLIENT, redirect_uri=REDIRECT,
        scopes=["https://graph.microsoft.com/Mail.Read", "offline_access"], state="xyz",
    )
    parsed = urlparse(url)
    assert parsed.path == f"/{TENANT}/oauth2/v2.0/authorize"
    q = parse_qs(parsed.query)
    assert q["client_id"] == [CLIENT]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == [REDIRECT]
    assert "offline_access" in q["scope"][0]        # required to receive a refresh token
    assert q["state"] == ["xyz"]


def test_generate_pkce_pair_challenge_is_s256_of_verifier():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected
    assert "=" not in challenge          # base64url, no padding


def test_authorize_url_includes_pkce_challenge_when_given():
    url = build_authorize_url(
        tenant=TENANT, client_id=CLIENT, redirect_uri=REDIRECT,
        scopes=["https://graph.microsoft.com/Mail.Read", "offline_access"], state="xyz",
        code_challenge="abc123",
    )
    q = parse_qs(urlparse(url).query)
    assert q["code_challenge"] == ["abc123"]
    assert q["code_challenge_method"] == ["S256"]


def test_token_request_body_includes_code_verifier_when_given():
    body = token_request_body(
        client_id=CLIENT, client_secret="s", code="c", redirect_uri=REDIRECT,
        scopes=["offline_access"], code_verifier="the-verifier",
    )
    assert body["code_verifier"] == "the-verifier"


def test_token_request_body_is_authorization_code_grant():
    body = token_request_body(
        client_id=CLIENT, client_secret="s3cret", code="the-code", redirect_uri=REDIRECT,
        scopes=["https://graph.microsoft.com/Mail.Read", "offline_access"],
    )
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "the-code"
    assert body["client_id"] == CLIENT
    assert body["client_secret"] == "s3cret"
    assert body["redirect_uri"] == REDIRECT


def test_refresh_request_body_is_refresh_token_grant():
    body = refresh_request_body(
        client_id=CLIENT, client_secret="s3cret", refresh_token="rt",
        scopes=["https://graph.microsoft.com/Mail.Read", "offline_access"],
    )
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "rt"


def test_token_provider_refreshes_and_caches_access_token():
    calls = []

    def poster(url: str, body: dict) -> dict:
        calls.append((url, body))
        return {"access_token": "AT-1", "expires_in": 3600}

    tp = DelegatedGraphTokenProvider(
        tenant=TENANT, client_id=CLIENT, client_secret="s", refresh_token="rt",
        poster=poster, now=lambda: 1000.0,
    )
    assert tp.get_token() == "AT-1"
    assert tp.get_token() == "AT-1"                 # cached — no second network call
    assert len(calls) == 1
    assert calls[0][0].endswith(f"/{TENANT}/oauth2/v2.0/token")
    assert calls[0][1]["grant_type"] == "refresh_token"


def test_token_provider_refetches_after_expiry():
    seq = iter([{"access_token": "AT-1", "expires_in": 100},
                {"access_token": "AT-2", "expires_in": 100}])
    clock = {"t": 1000.0}

    tp = DelegatedGraphTokenProvider(
        tenant=TENANT, client_id=CLIENT, client_secret="s", refresh_token="rt",
        poster=lambda url, body: next(seq), now=lambda: clock["t"],
    )
    assert tp.get_token() == "AT-1"
    clock["t"] += 200                                # past expiry (minus safety margin)
    assert tp.get_token() == "AT-2"


def test_token_provider_raises_on_error_response():
    tp = DelegatedGraphTokenProvider(
        tenant=TENANT, client_id=CLIENT, client_secret="s", refresh_token="rt",
        poster=lambda url, body: {"error": "invalid_grant", "error_description": "expired"},
        now=lambda: 1000.0,
    )
    with pytest.raises(RuntimeError):
        tp.get_token()


# --------------------------------------------------------------- app-only (client credentials)


def test_app_only_body_is_client_credentials_default_scope():
    from mailflow.adapters.graph.delegated_auth import app_only_request_body

    body = app_only_request_body(client_id=CLIENT, client_secret="s3cret")
    assert body["grant_type"] == "client_credentials"
    assert body["client_id"] == CLIENT
    assert body["client_secret"] == "s3cret"
    assert body["scope"] == "https://graph.microsoft.com/.default"


def test_app_only_token_provider_no_redirect_no_user():
    from mailflow.adapters.graph.delegated_auth import AppOnlyGraphTokenProvider

    calls = []

    def poster(url, body):
        calls.append(body)
        return {"access_token": "APP-AT", "expires_in": 3600}

    tp = AppOnlyGraphTokenProvider(
        tenant=TENANT, client_id=CLIENT, client_secret="s", poster=poster, now=lambda: 0.0
    )
    assert tp.get_token() == "APP-AT"
    assert tp.get_token() == "APP-AT"          # cached — no second call
    assert len(calls) == 1
    assert calls[0]["grant_type"] == "client_credentials"
