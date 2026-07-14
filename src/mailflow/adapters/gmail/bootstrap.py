"""Watch bootstrap + renewal. ensure a Gmail watch exists for each mailbox and seed
the cursor with the watch's starting historyId (so the first history diff has a start
point). renew_watches re-arms them (Gmail watches expire ~7 days; renew daily).

Pure helpers — no SDK; the live client is injected, so they're unit-testable."""

from __future__ import annotations

from typing import Any

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
        # REL-5 / INT-4: seed the cursor only on first-ever start. On a restart a cursor
        # already exists; re-seeding to the watch's CURRENT historyId would advance the
        # monotonic cursor past every message that arrived during downtime -> silent loss.
        if handle.history_id and cursor_store.get(tenant, stream) is None:
            cursor_store.commit_if_ahead(
                tenant, stream, Cursor(value=handle.history_id, order=int(handle.history_id))
            )
        handles.append(handle)
    return handles


def renew_watches(
    *, watch_manager: GmailWatchManager, handles: list[WatchHandle]
) -> list[WatchHandle]:
    return [watch_manager.renew_watch(h) for h in handles]


def should_schedule_renew(
    *, start_watch: bool, watch_renew_seconds: int, handles: list[WatchHandle]
) -> bool:
    """The watch-renewal daemon is armed only when the watch was started, the renew
    interval is positive, and at least one watch handle exists to renew. Extracted
    from run_service so the driver-scheduling decision is testable without running the
    blocking consume loop."""
    return bool(start_watch and watch_renew_seconds > 0 and handles)


def sweep_once(*, runtime: Any, client: Any, mailboxes: list[str]) -> None:
    """Safety-net poll (spec): per mailbox, submit a watermark so the stream is synced,
    then run the pipeline once — `fetch` diffs from the STORED cursor and catches anything
    a push notification missed. Idempotent (dedup claim + monotonic cursor make overlap
    with the push path harmless). `runtime` has `.provider.submit` + `.pipeline.run_once`."""
    for mailbox in mailboxes:
        history_id = str(client.get_profile(mailbox).get("historyId", "0"))
        runtime.provider.submit(mailbox, history_id)
    runtime.pipeline.run_once()
