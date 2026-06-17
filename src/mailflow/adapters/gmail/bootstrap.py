"""Watch bootstrap + renewal. ensure a Gmail watch exists for each mailbox and seed
the cursor with the watch's starting historyId (so the first history diff has a start
point). renew_watches re-arms them (Gmail watches expire ~7 days; renew daily).

Pure helpers — no SDK; the live client is injected, so they're unit-testable."""

from __future__ import annotations

from mailflow.adapters.gmail.watch import GmailWatchManager, WatchHandle
from mailflow.core.models import Cursor, StreamRef
from mailflow.core.ports import CursorStore


def bootstrap_watches(
    *, watch_manager: GmailWatchManager, cursor_store: CursorStore,
    tenant: str, mailboxes: list[str],
) -> list[WatchHandle]:
    handles: list[WatchHandle] = []
    for mailbox in mailboxes:
        stream = StreamRef(mailbox=mailbox, folder=None)
        handle = watch_manager.ensure_watch(stream)
        if handle.history_id:
            cursor_store.commit_if_ahead(
                tenant, stream, Cursor(value=handle.history_id, order=int(handle.history_id))
            )
        handles.append(handle)
    return handles


def renew_watches(
    *, watch_manager: GmailWatchManager, handles: list[WatchHandle]
) -> list[WatchHandle]:
    return [watch_manager.renew_watch(h) for h in handles]
