"""A6 (live wiring): build_gmail_runtime threads a ContentCleaner into the Pipeline,
so Gmail live emails get the same cleaning stage as the memory path."""

from __future__ import annotations

from typing import Any

from mailflow.adapters.gmail.composition import build_gmail_runtime
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.clean import ThinContentCleaner
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
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


def test_build_gmail_runtime_threads_cleaner_into_pipeline() -> None:
    cleaner = ThinContentCleaner()
    rt = build_gmail_runtime(
        gmail_cfg=GCFG, pubsub_cfg=PCFG, tenant="t",
        token_provider=_Token(), transport=_Transport(),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), cleaner=cleaner,
    )
    assert rt.pipeline.cleaner is cleaner
