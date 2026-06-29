"""Live bridge: capture Gmail → write each CleanEmail straight into emails.db.

This is what feeds the Email Intelligence UI (scripts/inbox_app.py), which reads
emails.db. Run this ALONGSIDE inbox_app.py, then send yourself a mail → it appears
in the UI within a few seconds.

It bypasses the separate clean-email Pub/Sub topic (which isn't provisioned) by
saving directly to the SQLite store the UI reads.

  python scripts/run_gmail_to_inbox.py     # Ctrl+C to stop
Needs the .env loaded (GMAIL_* + PUBSUB_* + GOOGLE_APPLICATION_CREDENTIALS).
"""

from __future__ import annotations

import os
from typing import Any

from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.live import run_service
from mailflow.emit.stdout import StdoutEmitter
from mailflow.filters.deterministic import BlockSenderFilter
from mailflow.persistence import SqliteEmailStore
from mailflow.secrets import EnvSecretProvider
from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore


class SqliteSaveEmitter:
    """Emitter that writes each emitted CleanEmail into emails.db (what the UI reads)."""

    def __init__(self, db_path: str) -> None:
        self.store = SqliteEmailStore(db_path)

    def emit(self, event: Any) -> None:
        ev = event.model_dump(by_alias=True, mode="json")  # 'from' alias + JSON-safe dates
        is_new = self.store.save(ev)
        subj = (ev.get("email") or {}).get("subject")
        print(("  SAVED -> " if is_new else "  dup   -> ") + str(subj), flush=True)


def main() -> None:
    mailboxes = [m.strip() for m in os.environ.get("GMAIL_MAILBOXES", "me").split(",") if m.strip()]
    gmail_cfg = GmailConfig.model_validate({
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret_ref": "env://GMAIL_CLIENT_SECRET",
        "oauth_refresh_token_ref": "env://GMAIL_REFRESH_TOKEN",
        "mailboxes": mailboxes,
    })
    ps = PubSubConfig(
        project_id=os.environ["PUBSUB_PROJECT_ID"],
        topic=os.environ.get("PUBSUB_TOPIC", "gmail-notifications"),
        subscription=os.environ["PUBSUB_SUBSCRIPTION"],
    )
    db = os.environ.get("EMAIL_DB", "emails.db")

    # Optional: block specific senders. Set BLOCKED_SENDERS="a@x.com,b@y.com".
    blocked = [a.strip() for a in os.environ.get("BLOCKED_SENDERS", "").split(",") if a.strip()]
    filters = [BlockSenderFilter(set(blocked))] if blocked else None
    if blocked:
        print(f"  filtering OUT senders: {blocked}", flush=True)

    print(f"Bridge: Gmail {mailboxes} -> {db}. Send a test email to verify. (Ctrl+C to stop)",
          flush=True)
    run_service(
        gmail_cfg=gmail_cfg, pubsub_cfg=ps, tenant="me",
        secret_provider=EnvSecretProvider(),
        emitter=SqliteSaveEmitter(db), dlq_emitter=StdoutEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=LocalBlobStore("attachments"),  # attachment bytes for the UI's download links
        filters=filters,
    )


if __name__ == "__main__":
    main()
