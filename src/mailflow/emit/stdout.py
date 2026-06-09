"""Stdout emitter — prints one JSON line per event (local/dev, spec §15 cuttable set)."""

from __future__ import annotations

import sys

from mailflow.core.events import EmailEvent
from mailflow.emit.memory import EmitReceipt


class StdoutEmitter:
    def emit(self, event: EmailEvent) -> EmitReceipt:
        sys.stdout.write(event.model_dump_json(by_alias=True) + "\n")
        return EmitReceipt(id=event.email.canonical_id, accepted=True)
