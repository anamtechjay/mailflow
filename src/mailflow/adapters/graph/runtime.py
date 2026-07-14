"""GraphEventHubsRuntime — realizes the per-email flow: Event Hub event ->
parse pointer -> submit to GraphProvider -> pipeline.run_once() -> checkpoint.
Defines minimal Protocols so the real azure-eventhub SDK stays out of the unit
suite. Checkpoint happens only after the pipeline run succeeds, so a failed run
leaves the event for redelivery."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mailflow.adapters.graph.lifecycle import GraphLifecycleHandler
from mailflow.adapters.graph.notifications import (
    parse_lifecycle_payload,
    parse_notification_payload,
)
from mailflow.adapters.graph.provider import GraphProvider
from mailflow.core.observability import RunReport


@runtime_checkable
class EventHubEvent(Protocol):
    def body_as_str(self) -> str: ...


@runtime_checkable
class Checkpointer(Protocol):
    def update(self, event: EventHubEvent) -> None: ...


@runtime_checkable
class PartitionContext(Protocol):
    """The slice of the azure-eventhub PartitionContext we use (duck-typed, so no
    azure import here)."""

    def update_checkpoint(self, event: Any) -> None: ...


class EventHubCheckpointer:
    """Adapts an azure-eventhub partition_context to the Checkpointer port. The
    consume loop builds one per received batch (the partition_context is per-batch)
    and passes it to process_batch."""

    def __init__(self, partition_context: PartitionContext) -> None:
        self._ctx = partition_context

    def update(self, event: EventHubEvent) -> None:
        self._ctx.update_checkpoint(event)


class NoopCheckpointer:
    """Instance-default checkpointer used when no per-batch one is supplied (e.g. in
    tests or before the live consume loop injects a real one)."""

    def update(self, event: EventHubEvent) -> None:
        return None


@runtime_checkable
class PipelineLike(Protocol):
    def run_once(self) -> object: ...


class GraphEventHubsRuntime:
    def __init__(
        self, *, provider: GraphProvider, pipeline: PipelineLike,
        checkpointer: Checkpointer, client_state: str,
        lifecycle_handler: GraphLifecycleHandler | None = None,
    ) -> None:
        self.provider = provider
        self.pipeline = pipeline
        self.checkpointer = checkpointer
        self.client_state = client_state
        self.lifecycle_handler = lifecycle_handler

    def process_batch(
        self, events: list[EventHubEvent], *, checkpointer: Checkpointer | None = None
    ) -> None:
        sink = checkpointer if checkpointer is not None else self.checkpointer
        for event in events:
            body = event.body_as_str()
            notes = parse_notification_payload(
                body, expected_client_state=self.client_state
            )
            if notes:
                for note in notes:
                    self.provider.submit(note)
                report = self.pipeline.run_once()
                # REL-8: if the run left any fetched message non-terminal (a transient
                # failure to be retried), do NOT checkpoint — hold the event so Event
                # Hubs redelivers it. Dedupe makes re-running already-done siblings safe.
                if isinstance(report, RunReport) and not report.all_terminal():
                    continue
            elif self.lifecycle_handler is not None:
                # lifecycle signals share the hub; react so ingestion doesn't stop
                # silently (renew/recreate/resync) — spec §8.5.
                for lifecycle in parse_lifecycle_payload(
                    body, expected_client_state=self.client_state
                ):
                    self.lifecycle_handler.handle(lifecycle)
            # checkpoint AFTER successful processing (or for ignored validation
            # events, which produce no notes) so failures get redelivered.
            sink.update(event)
