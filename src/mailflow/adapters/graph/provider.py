"""GraphProvider — notification-fed MailboxProvider. The Event Hubs runtime feeds
pointers via submit(); fetch() does the actual Graph GET per pending pointer and
yields RawMessages carrying the message JSON (with attachment metadata embedded
under '_attachments'). This keeps the core RawMessage shape unchanged."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.notifications import GraphNotification
from mailflow.adapters.graph.transport import GraphError
from mailflow.core.models import Cursor, RawMessage, StreamRef

# Resolve a subscription id to the StreamRef (mailbox + folder) it watches.
StreamResolver = Callable[[str], "StreamRef | None"]


class GraphProvider:
    PROVIDER = "graph"

    def __init__(
        self, *, client: GraphClient, stream_resolver: StreamResolver | None = None
    ) -> None:
        self.client = client
        self._stream_resolver = stream_resolver
        self._pending: list[GraphNotification] = []
        self._sweep_streams: list[StreamRef] = []
        self._internet_ids: dict[str, str] = {}
        self._order = 0

    # --- notification feed (called by the Event Hubs runtime) ---
    def submit(self, note: GraphNotification) -> None:
        self._pending.append(note)

    def submit_internet_id(self, message_id: str, internet_id: str) -> None:
        self._internet_ids[message_id] = internet_id

    def request_sweep(self, stream: StreamRef) -> None:
        """Queue a delta catch-up for a stream; the next fetch(stream) drains it.
        Used by the 'missed' lifecycle path to recover undelivered notifications."""
        if stream not in self._sweep_streams:
            self._sweep_streams.append(stream)

    def _stream_for(self, note: GraphNotification) -> StreamRef:
        # The basic notification carries no folder, but it does carry the
        # subscriptionId — and each subscription is per mailbox×folder. Resolve via
        # the subscription registry; fall back to inbox if unknown.
        if self._stream_resolver is not None:
            resolved = self._stream_resolver(note.subscription_id)
            if resolved is not None:
                return resolved
        return StreamRef(mailbox=note.user_id, folder="inbox")

    # --- MailboxProvider port ---
    def connect(self) -> None:
        return None

    def sync_streams(self) -> Iterable[StreamRef]:
        seen: list[StreamRef] = []
        for note in self._pending:
            s = self._stream_for(note)
            if s not in seen:
                seen.append(s)
        for s in self._sweep_streams:
            if s not in seen:
                seen.append(s)
        return seen

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        remaining: list[GraphNotification] = []
        for note in self._pending:
            if self._stream_for(note) != stream:
                remaining.append(note)
                continue
            data = self._fetch_message(note)
            if data is None:
                continue
            yield self._to_raw(stream, note, data)
        self._pending = remaining
        if stream in self._sweep_streams:
            self._sweep_streams.remove(stream)
            yield from self.sweep(stream, cursor)

    # --- delta catch-up (missed-event / restart safety net) ---
    def sweep(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        """Delta-replay a folder from the stored deltaLink (cursor.value). Yields a
        RawMessage per changed message; every message's cursor carries the new
        deltaLink so the pipeline persists the resume token. Idempotent: re-seen
        messages are caught by the dedupe store."""
        # Only a real @odata.deltaLink (a URL) is a resumable token; a synthetic
        # notification cursor means we have no delta token -> full resync (safe).
        delta_link = (
            cursor.value
            if cursor is not None and cursor.value.startswith("http")
            else None
        )
        messages, new_delta = self.client.delta_sweep(
            stream.mailbox, stream.folder or "inbox", delta_link
        )
        for data in messages:
            self._attach(data, stream.mailbox)
            self._order += 1
            raw_bytes = json.dumps(data).encode("utf-8")
            yield RawMessage(
                provider=self.PROVIDER,
                provider_message_id=str(data.get("id", "")),
                stream=stream,
                size_bytes=len(raw_bytes),
                received_at=datetime.now(timezone.utc),
                cursor=Cursor(value=new_delta, order=self._order),
                raw_bytes=raw_bytes,
                thread_key=str(data.get("conversationId", "") or ""),  # A7 parity w/ Gmail
            )

    def message_size(self, msg: RawMessage) -> int | None:
        try:
            data = json.loads(msg.raw_bytes or b"{}")
        except (ValueError, TypeError):
            return msg.size_bytes
        body = (data.get("body") or {}).get("content", "")
        att = sum(int(a.get("size", 0) or 0) for a in data.get("_attachments", []) or [])
        return att + len(str(body))

    # --- internals ---
    def _fetch_message(self, note: GraphNotification) -> dict[str, Any] | None:
        try:
            data = self.client.get_message(note.user_id, note.message_id)
        except GraphError as exc:
            if exc.status_code != 404:
                raise
            internet_id = self._internet_ids.get(note.message_id)
            if not internet_id:
                return None
            fallback = self.client.get_message_by_internet_id(note.user_id, internet_id)
            if fallback is None:
                return None
            data = fallback
        return self._attach(data, note.user_id)

    def _attach(self, data: dict[str, Any], user_id: str) -> dict[str, Any]:
        if data.get("hasAttachments"):
            data["_attachments"] = self.client.list_attachments(
                user_id, str(data.get("id", ""))
            )
        else:
            data["_attachments"] = []
        return data

    def _to_raw(self, stream: StreamRef, note: GraphNotification, data: dict[str, Any]) -> RawMessage:
        self._order += 1
        raw_bytes = json.dumps(data).encode("utf-8")
        return RawMessage(
            provider=self.PROVIDER,
            provider_message_id=str(data.get("id", note.message_id)),
            stream=stream,
            size_bytes=len(raw_bytes),
            received_at=datetime.now(timezone.utc),
            cursor=Cursor(value=f"{stream.key}#{self._order}", order=self._order),
            raw_bytes=raw_bytes,
            thread_key=str(data.get("conversationId", "") or ""),  # A7 parity w/ Gmail
        )
