"""Real SDK glue for the Graph + Event Hubs adapter.

The unit suite does NOT import this for its vendor deps: every Azure/MSAL/httpx
import is **local to a function**, so `import mailflow.adapters.graph.live` works
without the `graph` extra installed. Only calling `run_consume_loop` / `run_service`
(or constructing the token/transport providers) pulls in the SDKs.

Install the extra to run live:  pip install -e ".[graph]"

Auth is pluggable: pass either a connection string OR an azure-identity credential
(e.g. DefaultAzureCredential) to `run_consume_loop`. The operator supplies the
actual secret/key — nothing here is hardcoded.
"""

from __future__ import annotations

from typing import Any, Callable

from mailflow.adapters.graph.composition import build_graph_runtime
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.graph.runtime import (
    Checkpointer,
    EventHubCheckpointer,
    GraphEventHubsRuntime,
)
from mailflow.core.ports import (
    BlobStore,
    CursorStore,
    DedupeStore,
    Emitter,
    SecretProvider,
)

# Type of a per-batch checkpointer factory: partition_context -> Checkpointer.
CheckpointerFactory = Callable[[Any], Checkpointer]
BatchHandler = Callable[[Any, list[Any]], None]


class MsalTokenProvider:
    """App-only token via MSAL client credentials with in-memory caching."""

    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str, scope: str) -> None:
        import msal  # local import

        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )
        self._scope = scope

    def get_token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=[self._scope])
        if "access_token" not in result:
            raise RuntimeError(f"token error: {result.get('error_description', result)}")
        return str(result["access_token"])


class CredentialTokenProvider:
    """Wrap an azure-identity credential (e.g. DefaultAzureCredential) as a
    TokenProvider for a given scope. Used to give the CostTracker an ARM-scoped
    token (`https://management.azure.com/.default`). Duck-typed: no azure import."""

    def __init__(
        self, *, credential: Any, scope: str = "https://management.azure.com/.default"
    ) -> None:
        self._credential = credential
        self._scope = scope

    def get_token(self) -> str:
        return str(self._credential.get_token(self._scope).token)


class HttpxTransport:
    """HttpTransport backed by httpx."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        import httpx  # local import

        self._client = httpx.Client(timeout=timeout)

    def request(self, method: str, url: str, *, headers: dict[str, str], json: Any | None) -> Any:
        return self._client.request(method, url, headers=headers, json=json)


def _make_batch_handler(
    runtime: GraphEventHubsRuntime,
    checkpointer_factory: CheckpointerFactory = EventHubCheckpointer,
) -> BatchHandler:
    """Build the azure receive_batch callback. Extracted (and azure-free) so its
    logic is unit-testable: per received batch, run it through the runtime with a
    checkpointer bound to that batch's partition_context."""

    def on_event_batch(partition_context: Any, events: list[Any]) -> None:
        if not events:
            return
        runtime.process_batch(events, checkpointer=checkpointer_factory(partition_context))

    return on_event_batch


def run_consume_loop(
    *,
    runtime: GraphEventHubsRuntime,
    fully_qualified_namespace: str,
    hub: str,
    consumer_group: str = "$Default",
    connection_string: str | None = None,
    credential: Any | None = None,
    checkpoint_blob_account_url: str | None = None,
    checkpoint_connection_string: str | None = None,
    checkpoint_container: str | None = None,
    max_batch_size: int = 50,
    max_wait_time: float = 5.0,
) -> None:
    """Blocking sync azure-eventhub consume loop. Builds an EventHubConsumerClient
    (RBAC credential or connection string) with an optional blob checkpoint store
    and drives runtime.process_batch per received batch. Validation events
    (subscriptionId == 'NA') are skipped inside the runtime's notification parser.
    """
    if connection_string is None and credential is None:
        raise ValueError(
            "run_consume_loop needs either connection_string or credential "
            "(e.g. DefaultAzureCredential)"
        )

    from azure.eventhub import EventHubConsumerClient  # local import
    from azure.eventhub.extensions.checkpointstoreblob import (  # local import
        BlobCheckpointStore,
    )

    checkpoint_store = None
    if checkpoint_container and checkpoint_connection_string:
        checkpoint_store = BlobCheckpointStore.from_connection_string(
            checkpoint_connection_string, container_name=checkpoint_container
        )
    elif checkpoint_container and checkpoint_blob_account_url and credential is not None:
        checkpoint_store = BlobCheckpointStore(
            blob_account_url=checkpoint_blob_account_url,
            container_name=checkpoint_container,
            credential=credential,
        )

    if connection_string is not None:
        client = EventHubConsumerClient.from_connection_string(
            connection_string,
            consumer_group,
            eventhub_name=hub,
            checkpoint_store=checkpoint_store,
        )
    else:
        # reachable only when connection_string is None, so the guard above
        # guarantees a credential is present.
        assert credential is not None
        client = EventHubConsumerClient(
            fully_qualified_namespace=fully_qualified_namespace,
            eventhub_name=hub,
            consumer_group=consumer_group,
            credential=credential,
            checkpoint_store=checkpoint_store,
        )

    handler = _make_batch_handler(runtime)
    with client:
        client.receive_batch(
            on_event_batch=handler,
            max_batch_size=max_batch_size,
            max_wait_time=max_wait_time,
            starting_position="-1",  # from the start of the partition if no checkpoint
        )


def run_service(
    *,
    graph_cfg: GraphConfig,
    eventhub: EventHubConfig,
    tenant: str,
    secret_provider: SecretProvider,
    emitter: Emitter,
    dlq_emitter: Emitter,
    cursor_store: CursorStore,
    dedupe_store: DedupeStore,
    blob_store: BlobStore,
    connection_string: str | None = None,
    credential: Any | None = None,
    checkpoint_blob_account_url: str | None = None,
    checkpoint_connection_string: str | None = None,
    checkpoint_container: str | None = None,
) -> None:
    """Full live entrypoint: resolve the app secret, build the MSAL token provider +
    httpx transport, wire the runtime via the composition root, then run the consume
    loop. The operator supplies credentials/keys; nothing is hardcoded."""
    token_provider = MsalTokenProvider(
        tenant_id=graph_cfg.tenant_id,
        client_id=graph_cfg.client_id,
        client_secret=secret_provider.get(graph_cfg.client_secret_ref),
        scope=graph_cfg.scope,
    )
    runtime = build_graph_runtime(
        graph_cfg=graph_cfg,
        eventhub=eventhub,
        tenant=tenant,
        token_provider=token_provider,
        transport=HttpxTransport(),
        emitter=emitter,
        dlq_emitter=dlq_emitter,
        cursor_store=cursor_store,
        dedupe_store=dedupe_store,
        blob_store=blob_store,
    )
    run_consume_loop(
        runtime=runtime,
        fully_qualified_namespace=f"{eventhub.namespace}.servicebus.windows.net",
        hub=eventhub.hub,
        consumer_group=eventhub.consumer_group,
        connection_string=connection_string,
        credential=credential,
        checkpoint_blob_account_url=checkpoint_blob_account_url,
        checkpoint_connection_string=checkpoint_connection_string,
        checkpoint_container=checkpoint_container,
    )
