"""PubSubEmitter — publishes each EmailEvent (CleanEmail) as JSON to a Google Pub/Sub
topic, implementing the Emitter port. Your downstream app subscribes to that topic and
processes each email at its own pace (decoupled, durable buffer).

The publisher is injected (duck-typed) so unit tests run with a fake — no google SDK.
build_pubsub_emitter() creates the real PublisherClient (google import local)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mailflow.core.events import EmailEvent
from mailflow.emit.memory import EmitReceipt


@runtime_checkable
class _PublishFuture(Protocol):
    def result(self, timeout: float | None = None) -> str: ...


@runtime_checkable
class _Publisher(Protocol):
    def publish(self, topic: str, data: bytes, **attrs: str) -> _PublishFuture: ...


class PubSubEmitter:
    def __init__(self, *, publisher: _Publisher, topic_path: str) -> None:
        self._publisher = publisher
        self._topic_path = topic_path

    def emit(self, event: EmailEvent) -> EmitReceipt:
        data = event.model_dump_json(by_alias=True).encode("utf-8")
        future = self._publisher.publish(
            self._topic_path,
            data,
            tenant=event.tenant,
            schema_version=event.schema_version,
            canonical_id=event.email.canonical_id,
        )
        future.result()  # block until publish is confirmed; raises on failure
        return EmitReceipt(id=event.email.canonical_id, accepted=True)


def build_pubsub_emitter(
    *, project_id: str, topic: str, credentials: Any | None = None
) -> PubSubEmitter:
    """Create a live PubSubEmitter (real PublisherClient). google import is local so
    the unit suite never needs the SDK."""
    from google.cloud import pubsub_v1  # local import

    client_factory: Any = pubsub_v1.PublisherClient  # route through Any (untyped SDK ctor)
    publisher = client_factory(credentials=credentials) if credentials is not None else client_factory()
    topic_path = publisher.topic_path(project_id, topic)
    return PubSubEmitter(publisher=publisher, topic_path=topic_path)
