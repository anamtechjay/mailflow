"""Task 6 — attachment policy wiring through the live Gmail compose stack.

Asserts that build_gmail_runtime(..., attachment_policy=policy) threads the policy
into the MimeExtractor that the pipeline holds, and that the default path (no policy)
leaves attachment_policy as None.
"""

from __future__ import annotations

from typing import Any

from mailflow.adapters.gmail.composition import build_gmail_runtime
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule
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

    def force_refresh(self) -> None:
        pass


class _Transport:
    def request(self, method: str, url: str, *, headers: Any, json: Any) -> Any:  # pragma: no cover
        raise AssertionError("no HTTP in this wiring test")


def _runtime(attachment_policy: AttachmentPolicy | None = None) -> Any:
    return build_gmail_runtime(
        gmail_cfg=GCFG, pubsub_cfg=PCFG, tenant="t",
        token_provider=_Token(), transport=_Transport(),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), attachment_policy=attachment_policy,
    )


def test_attachment_policy_is_threaded_into_extractor() -> None:
    policy = AttachmentPolicy(
        real=AttachmentRule(max_bytes=1024),
        inline=AttachmentRule(max_bytes=512),
    )
    rt = _runtime(attachment_policy=policy)
    assert rt.pipeline.extractor.attachment_policy is policy


def test_default_attachment_policy_is_none() -> None:
    rt = _runtime()
    assert rt.pipeline.extractor.attachment_policy is None
