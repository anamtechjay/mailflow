"""A5 Gmail OIDC-JWT push verifier. The signature path uses google.oauth2.id_token (lazy
import), but the identity-shaping + rejection rules live behind an injectable `decoder`
seam so they are unit-tested deterministically without google-auth installed.

Also covers parse_pubsub_message's optional verifier hook: a failed verification drops
the notification (returns None); the no-verifier path is unchanged."""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from mailflow.adapters.gmail.notifications import parse_pubsub_message
from mailflow.adapters.gmail.webhook import GmailWebhookVerifier
from mailflow.core.errors import AuthError
from mailflow.core.models import WebhookIdentity

VALID_CLAIMS = {
    "iss": "https://accounts.google.com",
    "aud": "https://example.com/push",
    "email": "pubsub@project.iam.gserviceaccount.com",
    "email_verified": True,
}


def _verifier(decoder: Any, **kw: Any) -> GmailWebhookVerifier:
    return GmailWebhookVerifier(
        audience="https://example.com/push",
        subscription_id="sub-1",
        decoder=decoder,
        **kw,
    )


def _decoder_returning(claims: Mapping[str, Any]) -> Any:
    def _decode(token: str, audience: str) -> Mapping[str, Any]:
        return claims
    return _decode


def _headers(token: str = "good.jwt.token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_yields_identity() -> None:
    v = _verifier(_decoder_returning(VALID_CLAIMS))
    ident = v.verify(headers=_headers(), body=b"{}")
    assert isinstance(ident, WebhookIdentity)
    assert ident.provider == "gmail"
    assert ident.mailbox == "pubsub@project.iam.gserviceaccount.com"
    assert ident.subscription_id == "sub-1"


def test_missing_authorization_rejected() -> None:
    v = _verifier(_decoder_returning(VALID_CLAIMS))
    with pytest.raises(AuthError):
        v.verify(headers={}, body=b"{}")


def test_tampered_token_rejected() -> None:
    def _boom(token: str, audience: str) -> Mapping[str, Any]:
        raise ValueError("invalid signature")

    with pytest.raises(AuthError):
        _verifier(_boom).verify(headers=_headers(), body=b"{}")


def test_foreign_issuer_rejected() -> None:
    claims = dict(VALID_CLAIMS, iss="https://evil.example")
    with pytest.raises(AuthError):
        _verifier(_decoder_returning(claims)).verify(headers=_headers(), body=b"{}")


def test_wrong_audience_rejected() -> None:
    claims = dict(VALID_CLAIMS, aud="https://someone-else/push")
    with pytest.raises(AuthError):
        _verifier(_decoder_returning(claims)).verify(headers=_headers(), body=b"{}")


def test_foreign_service_account_rejected_when_restricted() -> None:
    v = _verifier(
        _decoder_returning(VALID_CLAIMS),
        allowed_emails={"only-this@project.iam.gserviceaccount.com"},
    )
    with pytest.raises(AuthError):
        v.verify(headers=_headers(), body=b"{}")


# ---- parse_pubsub_message verifier hook ----

class _OkVerifier:
    def verify(self, *, headers: Mapping[str, str], body: bytes) -> WebhookIdentity:
        return WebhookIdentity(provider="gmail", mailbox="x@y.com")


class _RejectVerifier:
    def verify(self, *, headers: Mapping[str, str], body: bytes) -> WebhookIdentity:
        raise AuthError("nope")


def test_parse_with_passing_verifier_returns_payload() -> None:
    body = b'{"emailAddress": "ops@acme.com", "historyId": 200}'
    out = parse_pubsub_message(body, verifier=_OkVerifier(), headers=_headers())
    assert out == ("ops@acme.com", 200)


def test_parse_with_failing_verifier_drops_message() -> None:
    body = b'{"emailAddress": "ops@acme.com", "historyId": 200}'
    out = parse_pubsub_message(body, verifier=_RejectVerifier(), headers=_headers())
    assert out is None


def test_parse_without_verifier_unchanged() -> None:
    body = b'{"emailAddress": "ops@acme.com", "historyId": 200}'
    assert parse_pubsub_message(body) == ("ops@acme.com", 200)
