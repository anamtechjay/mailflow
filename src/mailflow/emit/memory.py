"""In-memory emitter for tests and the zero-setup path."""

from __future__ import annotations

from pydantic import BaseModel

from mailflow.core.events import EmailEvent


class EmitReceipt(BaseModel):
    id: str
    accepted: bool = True


class MemoryEmitter:
    def __init__(self) -> None:
        self.events: list[EmailEvent] = []

    def emit(self, event: EmailEvent) -> EmitReceipt:
        self.events.append(event)
        return EmitReceipt(id=event.email.canonical_id, accepted=True)
