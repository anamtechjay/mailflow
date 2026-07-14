"""ServiceBusRuntime — realizes the per-message flow for Service Bus delivery:
SB message -> unwrap Event Grid CloudEvent -> GraphNotification -> submit to
GraphProvider -> pipeline.run_once() -> complete(message). Mirrors
GraphEventHubsRuntime but with per-message ack instead of offset checkpoint.

CD-1 (DLQ ownership): the pipeline owns message-level dead-lettering (dead_lettered +
dlq_emitter), so we COMPLETE after run_once() returns (success or handled-poison). We
only ABANDON (for redelivery) when run_once() RAISES — a transport/infra error — and we
re-raise so the consume loop can react. We never call Service Bus dead_letter() ourselves;
SB's MaxDeliveryCount DLQ is the infra backstop. One poison email => one mailflow DLQ record."""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from mailflow.adapters.graph.lifecycle import GraphLifecycleHandler
from mailflow.adapters.graph.notifications import parse_lifecycle_payload
from mailflow.adapters.graph.provider import GraphProvider
from mailflow.adapters.servicebus.eventgrid import parse_eventgrid_message, split_cloudevents
from mailflow.core.observability import RunReport


@runtime_checkable
class SbMessage(Protocol):
    def body_as_str(self) -> str: ...


@runtime_checkable
class SbReceiver(Protocol):
    def complete(self, message: Any) -> None: ...
    def abandon(self, message: Any) -> None: ...


@runtime_checkable
class PipelineLike(Protocol):
    def run_once(self) -> object: ...


def _lifecycle_collection(body: str) -> str:
    """Re-shape CloudEvents into the {'value':[...]} form parse_lifecycle_payload wants."""
    items = []
    for ev in split_cloudevents(body):
        data = ev.get("data") or {}
        items.append({
            "subscriptionId": data.get("subscriptionId", ""),
            "clientState": data.get("clientState", ""),
            "lifecycleEvent": data.get("lifecycleEvent", ""),
            "resource": data.get("resource") or ev.get("subject") or "",
        })
    return json.dumps({"value": items})


class ServiceBusRuntime:
    def __init__(
        self,
        *,
        provider: GraphProvider,
        pipeline: PipelineLike,
        client_state: str,
        lifecycle_handler: GraphLifecycleHandler | None = None,
    ) -> None:
        self.provider = provider
        self.pipeline = pipeline
        self.client_state = client_state
        self.lifecycle_handler = lifecycle_handler

    def process_messages(self, messages: list[SbMessage], *, receiver: SbReceiver) -> None:
        for message in messages:
            body = message.body_as_str()
            try:
                # Parsing happens inside the try too: an unexpected parse error (e.g. a
                # malformed CloudEvent shape the eventgrid guard doesn't already normalize
                # to []) is handled the same way as a pipeline error — abandon + re-raise —
                # instead of propagating uncaught and crash-looping the consumer.
                notes = parse_eventgrid_message(body, expected_client_state=self.client_state)
                if notes:
                    for note in notes:
                        self.provider.submit(note)
                    report = self.pipeline.run_once()
                    # REL-8: if the run left any fetched message non-terminal (a transient
                    # failure to be retried), abandon so SB redelivers rather than completing
                    # past it. Dedupe makes re-running already-done siblings harmless.
                    if isinstance(report, RunReport) and not report.all_terminal():
                        receiver.abandon(message)
                        continue
                elif self.lifecycle_handler is not None:
                    for lifecycle in parse_lifecycle_payload(
                        _lifecycle_collection(body), expected_client_state=self.client_state
                    ):
                        self.lifecycle_handler.handle(lifecycle)
            except Exception:
                receiver.abandon(message)   # not completed -> SB redelivers (CD-1)
                raise
            receiver.complete(message)      # ack after successful processing (CD-1)
