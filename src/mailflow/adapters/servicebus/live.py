"""Real SDK glue for the Service Bus + Event Grid adapter. Every azure/msal/httpx import
is LOCAL to a function, so `import mailflow.adapters.servicebus.live` works without the
`servicebus` (or `graph`) extra installed. Only calling run_service / constructing the
sender pulls in the SDKs.

Install the extra to run live:  pip install -e ".[servicebus,graph]"

Auth is pluggable: pass a connection string OR an azure-identity credential
(e.g. DefaultAzureCredential). The operator supplies the secret/key — nothing hardcoded.

Raw-message correspondence: ServiceBusRuntime.process_messages() (D1) calls
receiver.complete(message) / receiver.abandon(message) passing back the SAME object it
was handed in the message list — i.e. our SbMessage wrapper, NOT the raw azure message.
But azure-servicebus's receiver.complete_message()/abandon_message() need the RAW
ServiceBusReceivedMessage. So _MessageAdapter carries the raw message (`.raw`) and
_ReceiverAdapter unwraps it before calling into the azure receiver."""

from __future__ import annotations

from typing import Any

from mailflow.adapters.graph.config import GraphConfig
from mailflow.adapters.graph.live import HttpxTransport, MsalTokenProvider
from mailflow.adapters.servicebus.composition import build_servicebus_graph_runtime
from mailflow.adapters.servicebus.config import ServiceBusConfig
from mailflow.core.observability import Observers
from mailflow.core.ports import (
    BlobStore,
    ContentCleaner,
    CursorStore,
    DedupeStore,
    Emitter,
    Filter,
    SecretProvider,
)


class AzureServiceBusSender:
    """Adapts an azure-servicebus sender to the SbSender Protocol used by ServiceBusEmitter."""

    def __init__(self, sender: Any) -> None:
        self._sender = sender

    def send(
        self, *, body: bytes, message_id: str, content_type: str, session_id: str | None
    ) -> None:
        from azure.servicebus import ServiceBusMessage  # local import

        self._sender.send_messages(
            ServiceBusMessage(
                body, message_id=message_id, content_type=content_type, session_id=session_id
            )
        )


class _MessageAdapter:
    """Adapts a raw azure-servicebus received message to the runtime's SbMessage
    Protocol. Keeps the raw message reachable via `.raw` so `_ReceiverAdapter` can
    ack/abandon the actual azure object rather than this wrapper (see module docstring)."""

    def __init__(self, message: Any) -> None:
        self.raw = message

    def body_as_str(self) -> str:
        # ServiceBusReceivedMessage.body is either bytes or an iterable of bytes
        # chunks (uAMQP data sections) depending on how the message was composed.
        # Handle both rather than relying on __str__, whose body-decoding
        # behavior has varied across azure-servicebus versions.
        body = self.raw.body
        if isinstance(body, (bytes, bytearray)):
            return bytes(body).decode("utf-8")
        return b"".join(body).decode("utf-8")


class _ReceiverAdapter:
    """Adapts an azure-servicebus receiver to the runtime's SbReceiver Protocol.
    Unwraps `_MessageAdapter.raw` so the SDK gets the real ServiceBusReceivedMessage."""

    def __init__(self, receiver: Any) -> None:
        self._receiver = receiver

    def complete(self, message: _MessageAdapter) -> None:
        self._receiver.complete_message(message.raw)

    def abandon(self, message: _MessageAdapter) -> None:
        self._receiver.abandon_message(message.raw)


def run_service(
    *,
    graph_cfg: GraphConfig,
    servicebus_cfg: ServiceBusConfig,
    tenant: str,
    secret_provider: SecretProvider,
    emitter: Emitter,
    dlq_emitter: Emitter,
    cursor_store: CursorStore,
    dedupe_store: DedupeStore,
    blob_store: BlobStore,
    filters: list[Filter] | None = None,
    cleaner: ContentCleaner | None = None,
    on_filtered: str = "tag",
    connection_string: str | None = None,
    credential: Any | None = None,
    max_wait_time: float = 5.0,
    max_batch: int = 20,
    observers: Observers | None = None,
) -> None:
    """Full live entrypoint: build the Graph token provider + transport, build the
    ServiceBusRuntime, then block in a Service Bus receive loop feeding process_messages."""
    if connection_string is None and credential is None:
        raise ValueError("run_service needs either connection_string or credential")

    token_provider = MsalTokenProvider(
        tenant_id=graph_cfg.tenant_id,
        client_id=graph_cfg.client_id,
        client_secret=secret_provider.get(graph_cfg.client_secret_ref),
        scope=graph_cfg.scope,
    )
    runtime = build_servicebus_graph_runtime(
        graph_cfg=graph_cfg,
        tenant=tenant,
        token_provider=token_provider,
        transport=HttpxTransport(),
        emitter=emitter,
        dlq_emitter=dlq_emitter,
        cursor_store=cursor_store,
        dedupe_store=dedupe_store,
        blob_store=blob_store,
        filters=filters,
        cleaner=cleaner,
        on_filtered=on_filtered,  # type: ignore[arg-type]
        observers=observers,
    )

    from azure.servicebus import ServiceBusClient  # local import

    if connection_string is not None:
        client = ServiceBusClient.from_connection_string(connection_string)
    else:
        assert credential is not None
        client = ServiceBusClient(
            fully_qualified_namespace=servicebus_cfg.fully_qualified_namespace,
            credential=credential,
        )

    with client:
        with client.get_queue_receiver(queue_name=servicebus_cfg.entity_name) as receiver:
            sb_receiver = _ReceiverAdapter(receiver)
            while True:
                batch = receiver.receive_messages(
                    max_message_count=max_batch, max_wait_time=max_wait_time
                )
                if not batch:
                    continue
                runtime.process_messages(
                    [_MessageAdapter(m) for m in batch], receiver=sb_receiver
                )
