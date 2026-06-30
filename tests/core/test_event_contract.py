"""A4 — idempotency_key surfaced on EmailEvent + schema minor bump (Phase 0 contract)."""

from __future__ import annotations

from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail


def _email() -> CleanEmail:
    return CleanEmail(
        canonical_id="c1",
        provider="gmail",
        provider_message_id="m1",
        provider_stream_id="s1",
    )


def test_schema_version_minor_bumped_to_1_2() -> None:
    assert SCHEMA_VERSION == "1.2"


def test_email_event_carries_idempotency_key_default_empty() -> None:
    ev = EmailEvent(tenant="t", email=_email())
    assert ev.idempotency_key == ""


def test_idempotency_key_round_trips() -> None:
    ev = EmailEvent(tenant="t", email=_email(), idempotency_key="t|m@x|m1")
    assert ev.idempotency_key == "t|m@x|m1"
