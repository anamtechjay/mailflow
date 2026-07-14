"""connect_with_retry(): retries a flaky connect callable with backoff, then gives up
and raises the last error -- used by the Postgres stores at startup."""

from __future__ import annotations

import pytest

from mailflow.stores._retry import connect_with_retry


def test_succeeds_on_first_try() -> None:
    calls = []

    def connect() -> str:
        calls.append(1)
        return "ok"

    assert connect_with_retry(connect, attempts=3, sleep=lambda _: None) == "ok"
    assert len(calls) == 1


def test_retries_then_succeeds() -> None:
    calls = []

    def connect() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("transient")
        return "ok"

    slept = []
    result = connect_with_retry(connect, attempts=3, sleep=slept.append)
    assert result == "ok"
    assert len(calls) == 3
    assert len(slept) == 2  # slept between attempt 1->2 and 2->3, not after the last success


def test_gives_up_after_max_attempts() -> None:
    def connect() -> str:
        raise ConnectionError("always fails")

    with pytest.raises(ConnectionError, match="always fails"):
        connect_with_retry(connect, attempts=3, sleep=lambda _: None)
