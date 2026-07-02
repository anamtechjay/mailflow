"""Live test: watch Gmail with an ONLY-techjays.com filter and print each kept email.

Uses your existing .env (GMAIL_* + PUBSUB_* + GOOGLE_APPLICATION_CREDENTIALS).
Only mail FROM a techjays.com address reaches the printer; everything else is dropped
before it is emitted.

  python scripts/test_techjays_filter.py      # Ctrl+C to stop
"""

from __future__ import annotations

import os
from typing import Any

from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.live import run_service
from mailflow.auth import load_env_file
from mailflow.emit.stdout import StdoutEmitter
from mailflow.filters.deterministic import OnlyDomainFilter
from mailflow.secrets import EnvSecretProvider
from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore

ALLOW_DOMAINS = {"techjays.com"}   # <-- allow ONLY these sender domains


class PrintEmitter:
    """Print each email that PASSED the filter (i.e. a techjays.com sender)."""

    def emit(self, event: Any) -> None:
        email = event.email
        print(f"  KEPT -> {email.from_.address:28} | {email.subject}", flush=True)


def main() -> None:
    # load .env into the process environment (no PowerShell dance needed)
    for k, v in load_env_file(".env").items():
        os.environ.setdefault(k, v)

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

    print(f"Watching {mailboxes} — allow ONLY {ALLOW_DOMAINS}, drop everything else.")
    print("Send yourself mail FROM techjays.com (appears) and from gmail.com (dropped). Ctrl+C to stop.\n")

    run_service(
        gmail_cfg=gmail_cfg, pubsub_cfg=ps, tenant="techjays",
        secret_provider=EnvSecretProvider(),
        emitter=PrintEmitter(), dlq_emitter=StdoutEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=LocalBlobStore("attachments"),
        filters=[OnlyDomainFilter(ALLOW_DOMAINS)],   # <-- the filter
    )


if __name__ == "__main__":
    main()
