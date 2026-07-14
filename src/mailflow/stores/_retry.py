"""A small, dependency-free retry-with-backoff helper for store constructors that open a
real network connection (Postgres) — not needed by SQLite (a local file, no network)."""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def connect_with_retry(
    connect_fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call connect_fn() up to `attempts` times, with exponential backoff between
    attempts (base_delay, base_delay*2, ...). Raises the last exception if every
    attempt fails. Protects against a momentarily-unreachable Postgres server at
    process startup (e.g. a rolling restart) without needing an external dependency."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return connect_fn()
        except Exception as exc:  # noqa: BLE001 - any connect failure is retryable here
            last_exc = exc
            if attempt < attempts - 1:
                sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc
