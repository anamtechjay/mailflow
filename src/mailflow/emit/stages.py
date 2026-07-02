"""Composable post-processing stages (spec §5) + the custom clean hook (§6).

A stage is `fn(email: CleanEmail) -> CleanEmail | None | bool`:
  • return the email (or a modified one)  → continue to the next stage
  • return None / False                    → STOP — drop this email
  • return True                            → keep the current email, continue

Stages run AFTER extraction, BETWEEN the pipeline and the app's stream()/on_email — so
the core pipeline is untouched. `StagesEmitter` wraps the real emitter to run them.
"""

from __future__ import annotations

from typing import Callable, List

from mailflow.core.events import EmailEvent
from mailflow.core.models import CleanEmail

Stage = Callable[[CleanEmail], "CleanEmail | None | bool"]


def run_stages(email: CleanEmail, stages: List[Stage]) -> CleanEmail | None:
    """Thread the email through each stage; a falsy return drops it (returns None)."""
    for fn in stages:
        out = fn(email)
        if out is None or out is False:
            return None
        if isinstance(out, CleanEmail):
            email = out
        # out is True / other truthy -> keep the current email, continue
    return email


class StagesEmitter:
    """Run stages on each emitted email; forward kept emails to the inner emitter."""

    def __init__(self, inner: object, stages: List[Stage]) -> None:
        self._inner = inner
        self._stages = stages

    def emit(self, event: EmailEvent) -> object:
        email = run_stages(event.email, self._stages)
        if email is None:
            return None  # dropped by a stage
        if email is not event.email:
            event = event.model_copy(update={"email": email})
        return self._inner.emit(event)  # type: ignore[attr-defined]
