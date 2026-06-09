"""Zero-dependency in-memory MailboxProvider for tests and the `provider: memory`
zero-setup path (spec §12a)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Iterator

from mailflow.core.models import Cursor, RawMessage, StreamRef


@dataclass
class SeedEmail:
    provider_message_id: str
    raw: bytes
    received_at: datetime | None = None


class MemoryProvider:
    PROVIDER = "memory"

    def __init__(self, seed: dict[StreamRef, list[SeedEmail]]) -> None:
        self._seed = seed
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def sync_streams(self) -> Iterable[StreamRef]:
        return list(self._seed.keys())

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        start = cursor.order if cursor is not None else 0
        for index, item in enumerate(self._seed.get(stream, []), start=1):
            if index <= start:
                continue
            yield RawMessage(
                provider=self.PROVIDER,
                provider_message_id=item.provider_message_id,
                stream=stream,
                size_bytes=len(item.raw),
                received_at=item.received_at or datetime(2026, 6, 9, tzinfo=timezone.utc),
                cursor=Cursor(value=f"{stream.key}#{index}", order=index),
                raw_bytes=item.raw,
            )

    def message_size(self, msg: RawMessage) -> int | None:
        return msg.size_bytes
