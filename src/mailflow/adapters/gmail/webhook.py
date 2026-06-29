"""A5: verify a Gmail Pub/Sub *push* notification is genuine.

Push subscriptions carry an OIDC JWT in the `Authorization: Bearer <jwt>` header, signed
by Google for the audience you configured on the subscription. We verify the signature +
audience + issuer (and optionally the pushing service-account email), then return an
identity ONLY — never trusting the payload (wake-signal-only rule, A5).

The real signature check uses `google.oauth2.id_token` (imported lazily so this module
imports without google-auth). All identity-shaping + rejection logic sits behind the
`decoder` seam, which is deterministically unit-testable with a fake decoder.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from mailflow.core.errors import AuthError
from mailflow.core.models import WebhookIdentity

# decoder(token, audience) -> verified claims. Raises ValueError on a bad/forged token.
Decoder = Callable[[str, str], Mapping[str, Any]]

# Google issues either form depending on the token vintage.
_GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})


def _bearer(headers: Mapping[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "authorization":
            parts = value.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
                return parts[1]
            break
    raise AuthError("missing or malformed Authorization: Bearer header on push")


def _default_decoder(token: str, audience: str) -> Mapping[str, Any]:
    from google.auth.transport import requests as ga_requests  # local import
    from google.oauth2 import id_token  # local import

    verify: Any = id_token.verify_oauth2_token
    claims = verify(token, ga_requests.Request(), audience)
    assert isinstance(claims, dict)
    return claims


class GmailWebhookVerifier:
    def __init__(
        self,
        *,
        audience: str,
        issuer: str = "https://accounts.google.com",
        allowed_emails: set[str] | None = None,
        subscription_id: str | None = None,
        decoder: Decoder | None = None,
    ) -> None:
        self._audience = audience
        self._issuers = _GOOGLE_ISSUERS | {issuer}
        self._allowed_emails = allowed_emails
        self._subscription_id = subscription_id
        self._decoder = decoder or _default_decoder

    def verify(self, *, headers: Mapping[str, str], body: bytes) -> WebhookIdentity:
        token = _bearer(headers)
        try:
            claims = self._decoder(token, self._audience)
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - any decode failure (e.g. bad signature)
            raise AuthError(f"push token verification failed: {exc}") from exc

        if claims.get("iss") not in self._issuers:
            raise AuthError(f"push token from untrusted issuer: {claims.get('iss')!r}")
        if claims.get("aud") != self._audience:
            raise AuthError("push token audience mismatch")
        email = claims.get("email")
        if self._allowed_emails is not None and email not in self._allowed_emails:
            raise AuthError(f"push from unauthorized service account: {email!r}")

        return WebhookIdentity(
            provider="gmail",
            mailbox=str(email) if email is not None else None,
            subscription_id=self._subscription_id,
        )
