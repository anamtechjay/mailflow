"""Connectivity smoke test — run this AS SOON AS DevOps gives you the keys to confirm
everything works, before running the full service. Checks both auth layers:

  1. Gmail OAuth + read access  (users.getProfile for each mailbox)
  2. Pub/Sub subscriber access  (get_subscription on your subscription)

Setup:
  pip install -e ".[gmail]"
  # load .env into the terminal first (PowerShell):
  Get-Content .env | ? {$_ -match '=' -and -not $_.StartsWith('#')} | % { $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k.Trim(),$v.Trim()) }
  python scripts/check_gmail_connection.py

Reads env: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_MAILBOXES,
PUBSUB_PROJECT_ID, PUBSUB_SUBSCRIPTION  (+ GOOGLE_APPLICATION_CREDENTIALS for Pub/Sub).
"""

from __future__ import annotations

import os
import sys

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.live import HttpxTransport, OAuthTokenProvider
from mailflow.secrets import EnvSecretProvider


def _cfg() -> tuple[GmailConfig, PubSubConfig]:
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
    return gmail_cfg, ps


def main() -> int:
    sp = EnvSecretProvider()
    gmail_cfg, ps = _cfg()
    ok = True

    # 1. Gmail OAuth + read
    try:
        token = OAuthTokenProvider(
            client_id=gmail_cfg.client_id,
            client_secret=sp.get(gmail_cfg.client_secret_ref),
            refresh_token=sp.get(gmail_cfg.oauth_refresh_token_ref),
            token_uri=gmail_cfg.token_uri, scopes=gmail_cfg.scopes,
        )
        client = GmailClient(base_url=gmail_cfg.base_url, token_provider=token,
                             transport=HttpxTransport())
        for mbx in gmail_cfg.mailboxes:
            prof = client.get_profile(mbx)
            print(f"[OK]  Gmail read for {mbx}: historyId={prof.get('historyId')} "
                  f"messages={prof.get('messagesTotal')}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] Gmail read: {type(exc).__name__}: {exc}")

    # 2. Pub/Sub subscriber access — test the SAME permission the consumer uses
    # (pubsub.subscriptions.consume via pull), not get_subscription (a Viewer perm
    # the Subscriber role doesn't include). Pulled messages are left UNACKED.
    try:
        from google.cloud import pubsub_v1  # local import
        sub = pubsub_v1.SubscriberClient()
        path = sub.subscription_path(ps.project_id, ps.subscription)
        resp = sub.pull(
            request={"subscription": path, "max_messages": 1, "return_immediately": True},
            timeout=15,
        )
        print(f"[OK]  Pub/Sub consume OK on {path} "
              f"(pulled {len(resp.received_messages)} pending msg, left unacked)")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] Pub/Sub access: {type(exc).__name__}: {exc}")

    print("\n" + ("ALL CHECKS PASSED - keys are good, you can run the service."
                  if ok else "SOME CHECKS FAILED - fix the above before running."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
