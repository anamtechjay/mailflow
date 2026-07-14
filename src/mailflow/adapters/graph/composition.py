"""Composition root for the live Graph + Event Hubs processing stack.

Wires a GraphClient -> GraphProvider -> Pipeline -> GraphEventHubsRuntime from
injected pieces (token provider, HTTP transport, stores, emitter). Deliberately
imports NO vendor SDKs (azure/msal/httpx) so it stays unit-testable with fakes;
the real SDK glue lives in `live.py` and calls this.
"""

from __future__ import annotations

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.lifecycle import GraphLifecycleHandler
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.adapters.graph.provider import GraphProvider
from mailflow.adapters.graph.runtime import (
    Checkpointer,
    GraphEventHubsRuntime,
    NoopCheckpointer,
)
from mailflow.adapters.graph.subscriptions import GraphSubscriptionManager
from mailflow.adapters.graph.transport import HttpTransport, TokenProvider
from mailflow.core.observability import Observers
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.ports import (
    BlobStore,
    Classifier,
    ContentCleaner,
    CursorStore,
    DedupeStore,
    Emitter,
)
from mailflow.filters.chain import FilterChain
from mailflow.core.models import StreamRef
from mailflow.core.ports import Filter


def build_graph_runtime(
    *,
    graph_cfg: GraphConfig,
    eventhub: EventHubConfig,
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
    checkpointer: Checkpointer | None = None,
    subscription_manager: GraphSubscriptionManager | None = None,
    lifecycle_handler: GraphLifecycleHandler | None = None,
    observers: Observers | None = None,
) -> GraphEventHubsRuntime:
    client = GraphClient(
        base_url=graph_cfg.base_url,
        token_provider=token_provider,
        transport=transport,
        max_retries=graph_cfg.max_attempts,
    )
    # When a subscription manager is supplied, the provider resolves each
    # notification's folder via the subscriptionId -> stream registry.
    stream_resolver = (
        subscription_manager.stream_for_handle if subscription_manager is not None else None
    )
    provider = GraphProvider(client=client, stream_resolver=stream_resolver)
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
        config=PipelineConfig(tenant=tenant, max_attempts=graph_cfg.max_attempts),
        classifier=classifier,
        cleaner=cleaner,
        observers=observers,
    )
    # When a subscription manager is supplied, wire lifecycle handling: a 'missed'
    # event requests a delta sweep of the affected stream and re-runs the pipeline,
    # so undelivered mail is recovered end-to-end.
    if lifecycle_handler is None and subscription_manager is not None:
        def _resync(stream: StreamRef) -> None:
            provider.request_sweep(stream)
            pipeline.run_once()

        lifecycle_handler = GraphLifecycleHandler(
            subscription_manager=subscription_manager, resync=_resync
        )
    return GraphEventHubsRuntime(
        provider=provider,
        pipeline=pipeline,
        checkpointer=checkpointer or NoopCheckpointer(),
        client_state=graph_cfg.client_state,
        lifecycle_handler=lifecycle_handler,
    )
