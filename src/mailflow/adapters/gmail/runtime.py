"""GmailPubSubRuntime — drains Pub/Sub messages, feeds each watermark to the provider,
runs the pipeline, then acks. The ack IS the checkpoint: a message is acked only after
a successful run, so a failed run is redelivered. Minimal Protocols keep the real
google-cloud-pubsub SDK out of the unit suite."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mailflow.adapters.gmail.notifications import parse_pubsub_message
from mailflow.adapters.gmail.provider import GmailProvider


@runtime_checkable
class PubSubMessage(Protocol):
    @property
    def data(self) -> bytes: ...
    def ack(self) -> None: ...


@runtime_checkable
class PipelineLike(Protocol):
    def run_once(self) -> object: ...


class GmailPubSubRuntime:
    def __init__(self, *, provider: GmailProvider, pipeline: PipelineLike) -> None:
        self.provider = provider
        self.pipeline = pipeline

    def process_messages(self, messages: list[PubSubMessage]) -> None:
        for message in messages:
            parsed = parse_pubsub_message(message.data)
            if parsed is not None:
                email_address, history_id = parsed
                self.provider.submit(email_address, history_id)
                self.pipeline.run_once()
            # ack after a successful run (or for an unparseable message, which has
            # nothing to process) so failures are redelivered.
            message.ack()
