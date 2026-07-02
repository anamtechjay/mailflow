"""A7 thread_key on CleanEmail + A5 WebhookIdentity result shape (Phase 0 contract)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mailflow.core.models import CleanEmail, WebhookIdentity


def _email() -> CleanEmail:
    return CleanEmail(
        canonical_id="c1",
        provider="gmail",
        provider_message_id="m1",
        provider_stream_id="s1",
    )


def test_clean_email_has_thread_key_default_empty() -> None:
    assert _email().thread_key == ""


def test_clean_email_thread_key_round_trips() -> None:
    email = _email()
    email.thread_key = "thread-123"
    assert email.thread_key == "thread-123"


def test_webhook_identity_is_identity_only_with_optional_fields() -> None:
    wid = WebhookIdentity(provider="gmail")
    assert wid.provider == "gmail"
    assert wid.mailbox is None
    assert wid.stream_id is None
    assert wid.subscription_id is None


def test_webhook_identity_is_frozen() -> None:
    wid = WebhookIdentity(provider="gmail")
    with pytest.raises(ValidationError):
        wid.provider = "graph"  # type: ignore[misc]


def test_clean_email_classification_seam_defaults_false() -> None:
    e = _email()
    assert e.is_auto_submitted is False
    assert e.is_bounce is False


def test_envelope_classification_seam_defaults_false() -> None:
    from mailflow.core.models import Envelope, StreamRef
    env = Envelope(canonical_id="c", provider="memory", provider_message_id="m",
                   stream=StreamRef(mailbox="ops@acme.com"))
    assert env.is_auto_submitted is False
    assert env.is_bounce is False
