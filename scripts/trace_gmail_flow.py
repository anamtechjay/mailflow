"""Live TRACE of the full Gmail receive flow — prints the real data at each stage.

Run it, then send an email to your mailbox. You'll see, in order:
  STAGE 1  the raw Pub/Sub notification (what Google pushes)
  STAGE 2  it parsed into (emailAddress, historyId)
  STAGE 3  history.list result (which message IDs changed)
  STAGE 4  messages.get(raw) (the fetched message: size + base64 length)
  STAGE 5  the final CleanEmail (subject, from, body, attachments)

It also saves each CleanEmail to emails.db so the UI still updates.

  python scripts/trace_gmail_flow.py     # Ctrl+C to stop
"""

from __future__ import annotations

from typing import Any

from mailflow.adapters.gmail import client as client_mod
from mailflow.adapters.gmail import runtime as runtime_mod
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.live import run_service
from mailflow.emit.stdout import StdoutEmitter
from mailflow.persistence import SqliteEmailStore
from mailflow.secrets import EnvSecretProvider
from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore
import os

BAR = "=" * 70


# ---- STAGE 1 + 2: wrap the Pub/Sub notification parser ----
_orig_parse = runtime_mod.parse_pubsub_message
def _traced_parse(data: Any) -> Any:
    shown = data[:120] if isinstance(data, (bytes, str)) else data
    print(f"\n{BAR}\nSTAGE 1 — RAW Pub/Sub notification (what Google pushed):\n  {shown!r}")
    out = _orig_parse(data)
    print(f"STAGE 2 — parsed notification -> (emailAddress, historyId):\n  {out}")
    return out
runtime_mod.parse_pubsub_message = _traced_parse


# ---- STAGE 3: wrap history.list ----
_orig_hist = client_mod.GmailClient.history_message_ids
def _traced_hist(self: Any, user_id: str, start: str, label: str | None = None) -> Any:
    ids, latest = _orig_hist(self, user_id, start, label)
    print(f"STAGE 3 — history.list since historyId={start}:\n"
          f"  changed message IDs = {ids}\n  new cursor historyId = {latest}")
    return ids, latest
client_mod.GmailClient.history_message_ids = _traced_hist


# ---- STAGE 4: wrap messages.get(raw) ----
_orig_get = client_mod.GmailClient.get_message_raw
def _traced_get(self: Any, user_id: str, message_id: str) -> Any:
    data = _orig_get(self, user_id, message_id)
    print(f"STAGE 4 — messages.get(format=raw) id={message_id}:\n"
          f"  sizeEstimate = {data.get('sizeEstimate')} bytes\n"
          f"  raw (base64url) length = {len(str(data.get('raw', '')))} chars  "
          f"(this is the full RFC822 message, encoded)")
    return data
client_mod.GmailClient.get_message_raw = _traced_get


# ---- STAGE 5: emitter prints the final CleanEmail + saves to the UI's DB ----
class TraceEmitter:
    def __init__(self, db: str) -> None:
        self.store = SqliteEmailStore(db)

    def emit(self, event: Any) -> None:
        e = event.email
        print("STAGE 5 — final CleanEmail handed to your app:")
        print(f"  subject      = {e.subject!r}")
        print(f"  from         = {e.from_.address!r}")
        print(f"  to           = {[r.address for r in e.to]}")
        print(f"  direction    = {e.direction}")
        print(f"  body_text    = {e.body_text[:80]!r}...")
        print(f"  attachments  = {[(a.filename, a.size_bytes) for a in e.attachments]}")
        print(f"  canonical_id = {e.canonical_id}")
        print(BAR, flush=True)
        self.store.save(event.model_dump(by_alias=True, mode="json"))  # keep the UI updated


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
    print(f"TRACE running for {mailboxes}. Send a test email to watch the full flow.\n"
          f"(Ctrl+C to stop)", flush=True)
    run_service(
        gmail_cfg=gmail_cfg, pubsub_cfg=ps, tenant="me",
        secret_provider=EnvSecretProvider(),
        emitter=TraceEmitter(os.environ.get("EMAIL_DB", "emails.db")),
        dlq_emitter=StdoutEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=LocalBlobStore("attachments"),
    )


if __name__ == "__main__":
    main()
