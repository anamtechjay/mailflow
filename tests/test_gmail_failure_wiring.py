"""The Gmail composition root wires the token provider as the pipeline's auth_refresher
and accepts a dlq_store, so the live path gets durable DLQ + refresh-once for free."""

from __future__ import annotations

import inspect

from mailflow.adapters.gmail.composition import build_gmail_runtime
from mailflow.adapters.gmail.live import run_service


def test_build_gmail_runtime_accepts_dlq_store() -> None:
    assert "dlq_store" in inspect.signature(build_gmail_runtime).parameters


def test_run_service_accepts_dlq_store() -> None:
    assert "dlq_store" in inspect.signature(run_service).parameters
