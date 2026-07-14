"""F10 — Emit / sinks / projection (unit). See docs/qa-partA-coverage.md.

Field projection (`make_projection` / `connect(..., fields=[...])`), the `from`/`from_`
alias in wire serialization, and the ServiceBus emitter's long-message_id hashing.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from mailflow.adapters.servicebus.emitter import ServiceBusEmitter
from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail, Recipient
from mailflow.emit.stdout import StdoutEmitter
from mailflow.facade import make_projection


def _email(**kw: object) -> CleanEmail:
    base: dict[str, object] = dict(
        canonical_id="<m1@partner.com>",
        provider="memory",
        provider_message_id="m1",
        provider_stream_id="ops@acme.com|inbox",
        subject="hi",
        from_=Recipient(name="Alice", address="alice@partner.com"),
        schema_version=SCHEMA_VERSION,
    )
    base.update(kw)
    return CleanEmail.model_validate(base)


def _event(email: CleanEmail | None = None, *, idempotency_key: str = "acme|ops@acme.com|m1") -> EmailEvent:
    return EmailEvent(
        tenant="acme", ordering_key="ops@acme.com",
        idempotency_key=idempotency_key, email=email or _email(),
    )


def test_projection_subset() -> None:
    project = make_projection(["subject", "from"])
    out = project(_email())
    assert set(out.keys()) == {"subject", "from"}
    assert out["subject"] == "hi"
    assert out["from"].address == "alice@partner.com"


def test_projection_unknown_field_raises() -> None:
    with pytest.raises(ValueError):
        make_projection(["subject", "not_a_real_field"])


def test_from_alias_serialized(capsys: pytest.CaptureFixture[str]) -> None:
    StdoutEmitter().emit(_event())
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert "from" in payload["email"]
    assert "from_" not in payload["email"]
    assert payload["email"]["from"]["address"] == "alice@partner.com"


def test_servicebus_hashes_long_id() -> None:
    class _FakeSender:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        def send(self, *, body: bytes, message_id: str, content_type: str, session_id: str | None) -> None:
            self.sent.append({"message_id": message_id})

    short_key = "acme|ops@acme.com|m1"
    long_key = "acme|ops@acme.com|" + ("X" * 150)

    short_sender = _FakeSender()
    ServiceBusEmitter(sender=short_sender).emit(_event(idempotency_key=short_key))
    assert short_sender.sent[0]["message_id"] == short_key  # <=128 chars: unchanged

    long_sender = _FakeSender()
    ServiceBusEmitter(sender=long_sender).emit(_event(idempotency_key=long_key))
    hashed = str(long_sender.sent[0]["message_id"])
    assert hashed == hashlib.sha256(long_key.encode("utf-8")).hexdigest()
    assert len(hashed) <= 128
