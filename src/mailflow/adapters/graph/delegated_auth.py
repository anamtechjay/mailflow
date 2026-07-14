"""Delegated Microsoft Graph OAuth (authorization-code flow) — the Microsoft analogue of
`mailflow.auth.get_gmail_refresh_token`.

App-only Graph (client credentials, the `GraphProvider`/`MsalTokenProvider` path) needs
admin consent and reads any mailbox in the tenant. This module is the *delegated* path: a
**user signs in and consents**, we receive a refresh token, and can then read **that user's
own** mailbox (`/me/messages`) on their behalf. This is what "once the user gives permission
we can access their email" means.

Pure pieces (URL + request shaping, and the access-token provider) are unit-tested; the
interactive browser + local-redirect-server orchestration in `get_graph_refresh_token` is a
thin shell (not unit-tested, same policy as the Gmail InstalledAppFlow).

Azure prerequisites (one-time, in Entra ID → App registrations → your app):
  1. Authentication → add a **Web** (or Mobile/desktop) redirect URI: `http://localhost:8765/`.
  2. API permissions → add **delegated** `Mail.Read` (+ `offline_access`, `User.Read`);
     grant consent. (offline_access is what returns a refresh token.)
  3. A client secret (you already have it as SECRET_VALUE).

Only stdlib is used (urllib/http.server/webbrowser) so the auth CLI works without the
`graph` extra installed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import urllib.request
from typing import Callable

AUTHORITY = "https://login.microsoftonline.com"
GRAPH_MAIL_READ = "https://graph.microsoft.com/Mail.Read"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
DEFAULT_SCOPES = [GRAPH_MAIL_READ, "offline_access", "openid", "profile"]
DEFAULT_REDIRECT_PORT = 8765
# Refresh a little before the real expiry so an in-flight request never uses a dead token.
_EXPIRY_SAFETY_SECONDS = 60

Poster = Callable[[str, dict[str, str]], dict[str, object]]


def authorize_endpoint(tenant: str) -> str:
    return f"{AUTHORITY}/{tenant}/oauth2/v2.0/authorize"


def token_endpoint(tenant: str) -> str:
    return f"{AUTHORITY}/{tenant}/oauth2/v2.0/token"


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE (RFC 7636, S256).

    Many tenants now *require* Proof Key for Code Exchange on the authorization-code
    flow (Azure error AADSTS9002325 when it is missing). PKCE binds the auth code to a
    one-time secret so an intercepted code can't be redeemed by anyone else. It is safe
    to include even for confidential (Web) clients that also send a client secret."""
    verifier = secrets.token_urlsafe(48)  # ~64 url-safe chars, within the 43..128 range
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def build_authorize_url(
    *,
    tenant: str,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    code_challenge: str | None = None,
) -> str:
    """The URL to open in the user's browser for sign-in + consent."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(scopes),
        "state": state,
    }
    if code_challenge is not None:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return authorize_endpoint(tenant) + "?" + urllib.parse.urlencode(params)


def token_request_body(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    scopes: list[str],
    code_verifier: str | None = None,
) -> dict[str, str]:
    """POST body to exchange the authorization code for tokens (incl. a refresh token)."""
    body = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
    }
    if code_verifier is not None:
        body["code_verifier"] = code_verifier
    return body


def refresh_request_body(
    *, client_id: str, client_secret: str, refresh_token: str, scopes: list[str]
) -> dict[str, str]:
    """POST body to trade a refresh token for a fresh access token."""
    return {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "scope": " ".join(scopes),
    }


def app_only_request_body(*, client_id: str, client_secret: str) -> dict[str, str]:
    """POST body for the app-only (client-credentials) grant — NO redirect, NO user.

    Reads mail server-side using the app's own identity. Requires the tenant admin to
    have granted the **Application** `Mail.Read` permission with admin consent; the app
    then reads any mailbox it is authorized for via `/users/{mailbox}/messages`."""
    return {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": GRAPH_DEFAULT_SCOPE,
    }


def _urllib_poster(url: str, body: dict[str, str]) -> dict[str, object]:
    """Default form-encoded POST to the token endpoint using only stdlib."""
    data = urllib.parse.urlencode(body).encode("ascii")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed MS endpoint
            return dict(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:  # token endpoint returns JSON error bodies
        return dict(json.loads(exc.read().decode("utf-8")))


class DelegatedGraphTokenProvider:
    """A `TokenProvider` (has `get_token() -> str`) backed by a delegated refresh token.

    Exchanges the refresh token for an access token on demand and caches it until shortly
    before expiry, so it plugs into `GraphClient(token_provider=...)` exactly like the
    app-only `MsalTokenProvider` — but reads mail as the consenting user."""

    def __init__(
        self,
        *,
        tenant: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        scopes: list[str] | None = None,
        poster: Poster = _urllib_poster,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tenant = tenant
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._scopes = scopes or DEFAULT_SCOPES
        self._poster = poster
        self._now = now
        self._access_token = ""
        self._expires_at = 0.0

    def get_token(self) -> str:
        if self._access_token and self._now() < self._expires_at:
            return self._access_token
        result = self._poster(
            token_endpoint(self._tenant),
            refresh_request_body(
                client_id=self._client_id, client_secret=self._client_secret,
                refresh_token=self._refresh_token, scopes=self._scopes,
            ),
        )
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError(
                f"graph token refresh failed: "
                f"{result.get('error_description') or result.get('error') or result}"
            )
        expires_in = result.get("expires_in", 3600)
        ttl = float(expires_in) if isinstance(expires_in, (int, float, str)) else 3600.0
        self._access_token = token
        self._expires_at = self._now() + ttl - _EXPIRY_SAFETY_SECONDS
        # Microsoft may rotate the refresh token on each use — keep the newest.
        rotated = result.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            self._refresh_token = rotated
        return token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token


class AppOnlyGraphTokenProvider:
    """A `TokenProvider` for the app-only (client-credentials) flow — the fully
    server-side path with NO redirect URI and NO user sign-in. Mints an access token
    from the app's own identity and caches it until shortly before expiry, so it plugs
    into `GraphClient(token_provider=...)` the same way as the delegated provider."""

    def __init__(
        self,
        *,
        tenant: str,
        client_id: str,
        client_secret: str,
        poster: Poster = _urllib_poster,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tenant = tenant
        self._client_id = client_id
        self._client_secret = client_secret
        self._poster = poster
        self._now = now
        self._access_token = ""
        self._expires_at = 0.0

    def get_token(self) -> str:
        if self._access_token and self._now() < self._expires_at:
            return self._access_token
        result = self._poster(
            token_endpoint(self._tenant),
            app_only_request_body(client_id=self._client_id, client_secret=self._client_secret),
        )
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError(
                f"graph app-only token failed: "
                f"{result.get('error_description') or result.get('error') or result}"
            )
        expires_in = result.get("expires_in", 3600)
        ttl = float(expires_in) if isinstance(expires_in, (int, float, str)) else 3600.0
        self._access_token = token
        self._expires_at = self._now() + ttl - _EXPIRY_SAFETY_SECONDS
        return token


def get_graph_refresh_token(
    *,
    tenant: str,
    client_id: str,
    client_secret: str,
    port: int = DEFAULT_REDIRECT_PORT,
    scopes: list[str] | None = None,
    poster: Poster = _urllib_poster,
    open_browser: Callable[[str], object] | None = None,
) -> str:
    """Run the interactive consent flow and return a delegated refresh token.

    Opens the browser to Microsoft sign-in, captures the redirect `?code=...` on a one-shot
    local server, exchanges it for tokens, and returns the refresh token. `poster` and
    `open_browser` are injectable for testing; defaults use stdlib urllib + webbrowser."""
    import http.server
    import webbrowser

    used_scopes = scopes or DEFAULT_SCOPES
    redirect_uri = f"http://localhost:{port}/"
    state = "mailflow"
    code_verifier, code_challenge = generate_pkce_pair()
    auth_url = build_authorize_url(
        tenant=tenant, client_id=client_id, redirect_uri=redirect_uri,
        scopes=used_scopes, state=state, code_challenge=code_challenge,
    )

    captured: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required name
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            captured["code"] = (params.get("code") or [""])[0]
            captured["error"] = (params.get("error_description") or params.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>mailflow: you can close this tab and return to the terminal.</h2>"
            )

        def log_message(self, *_args: object) -> None:  # silence the default logging
            return

    (open_browser or webbrowser.open)(auth_url)
    server = http.server.HTTPServer(("localhost", port), _Handler)
    server.handle_request()  # blocks until the redirect hits us exactly once
    server.server_close()

    if captured.get("error"):
        raise RuntimeError(f"consent failed: {captured['error']}")
    code = captured.get("code")
    if not code:
        raise RuntimeError("no authorization code returned from Microsoft consent")

    result = poster(
        token_endpoint(tenant),
        token_request_body(
            client_id=client_id, client_secret=client_secret, code=code,
            redirect_uri=redirect_uri, scopes=used_scopes, code_verifier=code_verifier,
        ),
    )
    refresh = result.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise RuntimeError(
            f"no refresh token returned: "
            f"{result.get('error_description') or result.get('error') or result}"
        )
    return refresh
