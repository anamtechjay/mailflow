"""Output emitters that hand a CleanEmail back to the consuming app.

These are thin Emitter adapters (they satisfy `core.ports.Emitter`): the pipeline calls
`emit(event)` at the end of a successful run, and these route `event.email` to the app:

  CallbackEmitter -> calls a user function   (the `@mf.on_email` shape)
  QueueEmitter    -> buffers onto a queue     (the `for e in mf.stream()` shape)
"""

from __future__ import annotations

import queue
from typing import Callable, Iterator

from mailflow.core.events import EmailEvent
from mailflow.core.models import CleanEmail
from mailflow.emit.memory import EmitReceipt


class CallbackEmitter:
    """Invoke a user callback with each CleanEmail as it is emitted."""

    def __init__(self, fn: Callable[[CleanEmail], None]) -> None:
        self._fn = fn

    def emit(self, event: EmailEvent) -> EmitReceipt:
        self._fn(event.email)
        return EmitReceipt(id=event.email.canonical_id, accepted=True)


class QueueEmitter:
    """Buffer emitted CleanEmails onto a thread-safe queue.

    `drain_nowait()` returns everything currently buffered (finite/batch use). `items()`
    blocks, yielding emails until `close()` is called (live streaming use).
    """

    _SENTINEL = object()

    def __init__(self) -> None:
        self._q: "queue.Queue[object]" = queue.Queue()

    def emit(self, event: EmailEvent) -> EmitReceipt:
        self._q.put(event.email)
        return EmitReceipt(id=event.email.canonical_id, accepted=True)

    def drain_nowait(self) -> list[CleanEmail]:
        out: list[CleanEmail] = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, CleanEmail):
                out.append(item)
        return out

    def items(self) -> Iterator[CleanEmail]:
        while True:
            item = self._q.get()
            if item is self._SENTINEL:
                return
            if isinstance(item, CleanEmail):
                yield item

    def close(self) -> None:
        self._q.put(self._SENTINEL)
