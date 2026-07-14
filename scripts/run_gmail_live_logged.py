"""Live Gmail ingestion WITH Way-2 file logging AND the inbox-UI feed.

Same as run_gmail_live.py, but:
  * calls enable_logging(file="mailflow.log") first, so every message is written to the
    log file (Way 2), and
  * publishes each CleanEmail to CLEAN_EMAILS_TOPIC (Pub/Sub) when that env var is set,
    so scripts/inbox_app.py (localhost:5001) receives and displays it. Falls back to
    stdout if CLEAN_EMAILS_TOPIC is unset.

Run:
  python scripts/run_gmail_live_logged.py        # Ctrl+C to stop
"""

from __future__ import annotations

import os

from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.live import run_service
from mailflow.emit.stdout import StdoutEmitter
from mailflow.logging_setup import enable_logging
from mailflow.secrets import EnvSecretProvider
from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore

LOG_FILE = os.path.join(os.getcwd(), "mailflow.log")


def load_env(path: str = ".env") -> None:
    """Load KEY=VALUE lines from .env into the environment (existing vars win)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    load_env()

    # --- Way 2: write every pipeline decision to ./mailflow.log ---
    enable_logging(level="INFO", file=LOG_FILE)
    print(f"[logging] writing pipeline logs to: {LOG_FILE}")

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

    # --- feed the inbox UI: publish each CleanEmail to CLEAN_EMAILS_TOPIC ---
    clean_topic = os.environ.get("CLEAN_EMAILS_TOPIC")
    if clean_topic:
        from mailflow.emit.pubsub import build_pubsub_emitter
        emitter = build_pubsub_emitter(project_id=ps.project_id, topic=clean_topic)
        print(f"[emit] publishing CleanEmails to Pub/Sub topic '{clean_topic}' "
              f"-> inbox_app (localhost:5001) will show them")
    else:
        emitter = StdoutEmitter()
        print("[emit] CLEAN_EMAILS_TOPIC not set -> printing to stdout only "
              "(the inbox UI will NOT receive anything)")

    print(f"Starting Gmail ingestion for {mailboxes} (Ctrl+C to stop). "
          f"Send a test email to verify...")
    run_service(
        gmail_cfg=gmail_cfg, pubsub_cfg=ps, tenant="me",
        secret_provider=EnvSecretProvider(),
        emitter=emitter, dlq_emitter=StdoutEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=LocalBlobStore("attachments"),
    )


if __name__ == "__main__":
    main()
