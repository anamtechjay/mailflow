"""Composition root for the live Gmail processing stack. Wires GmailClient ->
GmailProvider -> Pipeline -> GmailPubSubRuntime. The pipeline uses the CORE
MimeEnvelopeParser + MimeExtractor (Gmail returns raw RFC822), so there is no
Gmail-specific parser/extractor. Imports NO vendor SDK — that lives in live.py."""

from __future__ import annotations

from typing import Literal

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.provider import GmailProvider
from mailflow.adapters.gmail.runtime import GmailPubSubRuntime
from mailflow.adapters.gmail.transport import HttpTransport, RefreshableTokenProvider
from mailflow.core.observability import Observers
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.ports import (
    BlobStore,
    Classifier,
    ContentCleaner,
    CursorStore,
    DeadLetterStore,
    DedupeStore,
    Emitter,
    Filter,
)
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.policy import AttachmentPolicy
from mailflow.filters.chain import FilterChain


def build_gmail_runtime(
    *,
    gmail_cfg: GmailConfig,
    pubsub_cfg: PubSubConfig,
    tenant: str,
    token_provider: RefreshableTokenProvider,
    transport: HttpTransport,
    emitter: Emitter,
    dlq_emitter: Emitter,
    cursor_store: CursorStore,
    dedupe_store: DedupeStore,
    blob_store: BlobStore,
    filters: list[Filter] | None = None,
    classifier: Classifier | None = None,
    cleaner: ContentCleaner | None = None,
    dlq_store: DeadLetterStore | None = None,
    attachment_policy: AttachmentPolicy | None = None,
    on_filtered: Literal["tag", "drop"] = "tag",
    observers: Observers | None = None,
) -> GmailPubSubRuntime:
    client = GmailClient(
        base_url=gmail_cfg.base_url,
        token_provider=token_provider,
        transport=transport,
        max_retries=gmail_cfg.max_attempts,
    )
    label = gmail_cfg.label_ids[0] if gmail_cfg.label_ids else None
    provider = GmailProvider(
        client=client, label_id=label, cursor_store=cursor_store, tenant=tenant,
    )
    pipeline = Pipeline(
        provider=provider,
        parser=MimeEnvelopeParser(),
        filters=FilterChain(filters or []),
        extractor=MimeExtractor(attachment_policy=attachment_policy),
        emitter=emitter,
        dlq_emitter=dlq_emitter,
        cursor_store=cursor_store,
        dedupe_store=dedupe_store,
        blob_store=blob_store,
        config=PipelineConfig(tenant=tenant, max_attempts=gmail_cfg.max_attempts, on_filtered=on_filtered),
        classifier=classifier,
        cleaner=cleaner,
        auth_refresher=token_provider,
        dlq_store=dlq_store,
        observers=observers,
    )
    return GmailPubSubRuntime(provider=provider, pipeline=pipeline)
