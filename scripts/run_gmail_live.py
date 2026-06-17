"""Run the live Gmail ingestion service once the keys are in .env. Starts the watch
(seeds the cursor), then consumes Pub/Sub and prints each emitted CleanEmail to stdout
so you can SEE mail flowing in. Send yourself a test email to verify.

Setup:
  pip install -e ".[gmail]"
  # load .env (PowerShell), then:
  python scripts/run_gmail_live.py     # Ctrl+C to stop

Emits to stdout for the demo; swap StdoutEmitter for your real transport in production.
"""

from __future__ import annotations

import os

from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.live import run_service
from mailflow.emit.stdout import StdoutEmitter
from mailflow.secrets import EnvSecretProvider
from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore


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
    # If CLEAN_EMAILS_TOPIC is set, publish each CleanEmail to that Pub/Sub topic
    # (your app subscribes to it); otherwise print to stdout for a quick test.
    clean_topic = os.environ.get("CLEAN_EMAILS_TOPIC")
    if clean_topic:
        from mailflow.emit.pubsub import build_pubsub_emitter
        emitter = build_pubsub_emitter(project_id=ps.project_id, topic=clean_topic)
        print(f"Publishing CleanEmails to Pub/Sub topic: {clean_topic}")
    else:
        emitter = StdoutEmitter()
        print("Emitting to stdout (set CLEAN_EMAILS_TOPIC to publish to Pub/Sub instead)")

    print(f"Starting Gmail ingestion for {mailboxes} (Ctrl+C to stop). "
          f"Send a test email to verify…")
    run_service(
        gmail_cfg=gmail_cfg, pubsub_cfg=ps, tenant="me",
        secret_provider=EnvSecretProvider(),
        emitter=emitter, dlq_emitter=StdoutEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=LocalBlobStore("attachments"),   # attachment files saved here
    )


if __name__ == "__main__":
    main()
