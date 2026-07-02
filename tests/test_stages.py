"""Composable post-processing stages (spec §5) + custom clean (§6)."""

from __future__ import annotations

from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail
from mailflow.emit.memory import MemoryEmitter
from mailflow.emit.stages import StagesEmitter, run_stages


def _email(subject: str = "hi") -> CleanEmail:
    return CleanEmail(
        canonical_id="c", provider="memory", provider_message_id="m",
        provider_stream_id="s", subject=subject,
    )


def _event(email: CleanEmail) -> EmailEvent:
    return EmailEvent(schema_version=SCHEMA_VERSION, tenant="t", ordering_key="k", email=email)


def test_stage_modifies_email() -> None:
    def tag(e: CleanEmail) -> CleanEmail:
        e.subject = e.subject + " [tagged]"
        return e
    out = run_stages(_email(), [tag])
    assert out is not None and out.subject == "hi [tagged]"


def test_stage_falsy_drops() -> None:
    assert run_stages(_email(), [lambda e: None]) is None
    assert run_stages(_email(), [lambda e: False]) is None


def test_stages_run_in_order() -> None:
    seen: list[int] = []
    run_stages(_email(), [lambda e: (seen.append(1), e)[1], lambda e: (seen.append(2), e)[1]])
    assert seen == [1, 2]


def test_stages_emitter_forwards_kept_and_drops_others() -> None:
    inner = MemoryEmitter()
    se = StagesEmitter(inner, [lambda e: e if e.subject == "keep" else None])
    se.emit(_event(_email("keep")))
    se.emit(_event(_email("drop")))
    assert [ev.email.subject for ev in inner.events] == ["keep"]


def test_stages_emitter_propagates_modification() -> None:
    inner = MemoryEmitter()
    def tag(e: CleanEmail) -> CleanEmail:
        return e.model_copy(update={"subject": "X"})
    se = StagesEmitter(inner, [tag])
    se.emit(_event(_email("orig")))
    assert inner.events[0].email.subject == "X"
