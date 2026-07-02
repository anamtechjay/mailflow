"""IntervalScheduler — run background maintenance tasks on a timer alongside the
blocking consume loop (Gmail watch renewal + the safety-net sweep).

The task functions (renew_watches / sweep_once) are pure and unit-tested; this is only
the thin thread/sleep glue. A failing tick is swallowed so the loop never dies.
"""

from __future__ import annotations

import threading
from typing import Callable


class IntervalScheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def every(self, seconds: float, fn: Callable[[], object], name: str = "task") -> None:
        """Run `fn` every `seconds` on a daemon thread until stop() is called.
        The return value of `fn` is ignored."""
        def loop() -> None:
            while not self._stop.wait(seconds):
                try:
                    fn()
                except Exception:  # noqa: BLE001 - a tick failure must not kill the loop
                    pass
        thread = threading.Thread(target=loop, daemon=True, name=name)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
