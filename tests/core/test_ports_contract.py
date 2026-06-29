"""A5/A6/A8 new ports + A11 stability markers & SYNC_ONLY (Phase 0 contract)."""

from __future__ import annotations

from mailflow.core.models import CleanEmail
from mailflow.core.ports import (
    SYNC_ONLY,
    ContentCleaner,
    MailboxProvider,
    SecretProvider,
    TokenRotationSink,
    WebhookVerifier,
)


def test_sync_only_policy_declared_true() -> None:
    assert SYNC_ONLY is True


def test_content_cleaner_is_runtime_checkable() -> None:
    class _Cleaner:
        def clean(self, email: CleanEmail) -> CleanEmail:
            return email

    class _NotACleaner:
        pass

    assert isinstance(_Cleaner(), ContentCleaner)
    assert not isinstance(_NotACleaner(), ContentCleaner)


def test_webhook_verifier_is_runtime_checkable() -> None:
    class _Verifier:
        def verify(self, *, headers: dict[str, str], body: bytes) -> object:
            return None

    assert isinstance(_Verifier(), WebhookVerifier)
    assert not isinstance(object(), WebhookVerifier)


def test_token_rotation_sink_is_runtime_checkable() -> None:
    class _Sink:
        def on_refresh(self, ref: str, new_token: str) -> None:
            return None

    assert isinstance(_Sink(), TokenRotationSink)
    assert not isinstance(object(), TokenRotationSink)


def test_existing_ports_marked_stable() -> None:
    assert MailboxProvider.__stability__ == "stable"
    assert SecretProvider.__stability__ == "stable"


def test_new_ports_marked_provisional() -> None:
    for proto in (WebhookVerifier, ContentCleaner, TokenRotationSink):
        assert proto.__stability__ == "provisional"
