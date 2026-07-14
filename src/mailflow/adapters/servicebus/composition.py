"""Composition root for the Service Bus + Event Grid ingress stack. Wires GraphClient ->
GraphProvider -> Pipeline -> ServiceBusRuntime from injected pieces. Imports NO vendor SDK
(azure/msal/httpx) — the real glue is in live.py. Reuses the Graph provider/parser/extractor
unchanged; only the transport runtime differs from the Event Hubs path."""

from __future__ import annotations

from typing import Literal

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.config import GraphConfig
from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.adapters.graph.provider import GraphProvider
from mailflow.adapters.graph.transport import HttpTransport, TokenProvider
from mailflow.adapters.servicebus.runtime import ServiceBusRuntime
from mailflow.core.observability import Observers
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.ports import (
    BlobStore,
    Classifier,
    ContentCleaner,
    CursorStore,
    DedupeStore,
    Emitter,
    Filter,
)
from mailflow.filters.chain import FilterChain


def build_servicebus_graph_runtime(
    *,
    graph_cfg: GraphConfig,
    tenant: str,
    token_provider: TokenProvider,
    transport: HttpTransport,
    emitter: Emitter,
    dlq_emitter: Emitter,
    cursor_store: CursorStore,
    dedupe_store: DedupeStore,
    blob_store: BlobStore,
    filters: list[Filter] | None = None,
    classifier: Classifier | None = None,
    cleaner: ContentCleaner | None = None,
    on_filtered: Literal["tag", "drop"] = "tag",
    observers: Observers | None = None,
) -> ServiceBusRuntime:
    client = GraphClient(
        base_url=graph_cfg.base_url,
        token_provider=token_provider,
        transport=transport,
        max_retries=graph_cfg.max_attempts,
    )
    provider = GraphProvider(client=client)
    pipeline = Pipeline(
        provider=provider,
        parser=GraphEnvelopeParser(),
        filters=FilterChain(filters or []),
        extractor=GraphExtractor(),
        emitter=emitter,
        dlq_emitter=dlq_emitter,
        cursor_store=cursor_store,
        dedupe_store=dedupe_store,
        blob_store=blob_store,
        config=PipelineConfig(
            tenant=tenant, max_attempts=graph_cfg.max_attempts, on_filtered=on_filtered
        ),
        classifier=classifier,
        cleaner=cleaner,
        observers=observers,
    )
    return ServiceBusRuntime(
        provider=provider, pipeline=pipeline, client_state=graph_cfg.client_state
    )
