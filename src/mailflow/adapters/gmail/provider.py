"""GmailProvider — notification-fed MailboxProvider. The Pub/Sub runtime feeds a
historyId via submit(); fetch() diffs the mailbox from the stored cursor
(history.list) and yields a RawMessage per changed message carrying the RAW RFC822
bytes (messages.get format=raw). The pipeline's extractor seam then dispatches to the
core MimeExtractor — so Gmail needs no provider-specific parser/extractor."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Iterable, Iterator

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.transport import StaleHistoryError
from mailflow.core.errors import PermanentError
from mailflow.core.models import Cursor, RawMessage, StreamRef
from mailflow.core.ports import CursorStore


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + pad)
    except (binascii.Error, ValueError) as exc:
        # B3: a malformed raw payload will never decode -> poison message, DLQ (no retry).
        raise PermanentError(f"invalid base64 in gmail raw payload: {exc}") from exc


class GmailProvider:
    PROVIDER = "gmail"

    def __init__(
        self, *, client: GmailClient, label_id: str | None = "INBOX",
        cursor_store: CursorStore | None = None, tenant: str = "",
    ) -> None:
        self.client = client
        self.label_id = label_id
        self._cursor_store = cursor_store      # for stale-historyId (404) self-heal
        self._tenant = tenant
        self._pending: dict[str, str] = {}  # mailbox -> latest submitted historyId

    # --- notification feed (called by the Pub/Sub runtime) ---
    def submit(self, email_address: str, history_id: str | int) -> None:
        self._pending[email_address] = str(history_id)

    # --- MailboxProvider port ---
    def connect(self) -> None:
        return None

    def sync_streams(self) -> Iterable[StreamRef]:
        return [StreamRef(mailbox=m, folder=None) for m in self._pending]

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        # Diff from the stored cursor; on first notification (no cursor yet) fall back
        # to the submitted historyId so we don't full-resync the whole mailbox.
        start = cursor.value if cursor is not None else self._pending.get(stream.mailbox)
        if not start:
            self._pending.pop(stream.mailbox, None)
            return
        try:
            ids, latest = self.client.history_message_ids(stream.mailbox, start, self.label_id)
        except StaleHistoryError:
            # The stored historyId is too old. Re-seed to the current historyId so the
            # mailbox isn't stuck (the gap mail is skipped — the safe recovery).
            self._reseed(stream)
            self._pending.pop(stream.mailbox, None)
            return
        new_cursor = Cursor(value=latest, order=int(latest))
        for message_id in ids:
            try:
                data = self.client.get_message_raw(stream.mailbox, message_id)
                raw = _b64url_decode(str(data.get("raw", "")))
            except PermanentError:
                # B2: this one record is poison (404/410/invalid base64). Skip it so the
                # rest of the batch still flows; AuthError/TransientError are whole-stream
                # problems and propagate (the cursor must not advance past unread mail).
                continue
            yield RawMessage(
                provider=self.PROVIDER,
                provider_message_id=message_id,
                stream=stream,
                size_bytes=int(data.get("sizeEstimate", len(raw)) or len(raw)),
                received_at=datetime.now(timezone.utc),
                cursor=new_cursor,
                raw_bytes=raw,
                thread_key=str(data.get("threadId", "")),  # A7: Gmail conversation id
            )
        self._pending.pop(stream.mailbox, None)

    def _reseed(self, stream: StreamRef) -> None:
        """On a stale historyId, advance the stored cursor to the current historyId."""
        if self._cursor_store is None:
            return
        current = str(self.client.get_profile(stream.mailbox).get("historyId", ""))
        if current:
            self._cursor_store.commit_if_ahead(
                self._tenant, stream, Cursor(value=current, order=int(current))
            )

    def message_size(self, msg: RawMessage) -> int | None:
        return msg.size_bytes
