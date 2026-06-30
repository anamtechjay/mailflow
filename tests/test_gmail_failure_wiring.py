"""The Gmail composition root wires the token provider as the pipeline's auth_refresher
and threads a dlq_store into the constructed Pipeline, so the live path gets durable
DLQ + refresh-once for free."""

from __future__ import annotations

import inspect
from typing import Any

from mailflow.adapters.gmail.composition import build_gmail_runtime
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.live import run_service
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDeadLetterStore,
    InMemoryDedupeStore,
)

GCFG = GmailConfig(
    client_id="cid", client_secret_ref="cs", oauth_refresh_token_ref="rt", mailboxes=["me"]
)
PCFG = PubSubConfig(project_id="p", topic="t", subscription="s")


class _Token:
    def get_token(self) -> str:
        return "tok"

    def force_refresh(self) -> None:  # satisfies RefreshableTokenProvider/AuthRefresher
        pass


class _Transport:
    def request(self, method: str, url: str, *, headers: Any, json: Any) -> Any:  # pragma: no cover
        raise AssertionError("no HTTP in this wiring test")


def test_build_gmail_runtime_threads_dlq_store_and_auth_refresher() -> None:
    token_provider = _Token()
    dlq_store = InMemoryDeadLetterStore()
    rt = build_gmail_runtime(
        gmail_cfg=GCFG, pubsub_cfg=PCFG, tenant="t",
        token_provider=token_provider, transport=_Transport(),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), dlq_store=dlq_store,
    )
    assert rt.pipeline.dlq_store is dlq_store
    assert rt.pipeline.auth_refresher is token_provider


def test_run_service_accepts_dlq_store() -> None:
    assert "dlq_store" in inspect.signature(run_service).parameters
