"""mailflow command-line interface.

    mailflow auth gmail     one-time: browser OAuth -> save GMAIL_REFRESH_TOKEN to .env
    mailflow check gmail    verify the saved Gmail (+ Pub/Sub) credentials

Works identically as the installed `mailflow` command or as `python -m mailflow ...`
(the always-works fallback when the entry-point isn't on PATH).
"""

from __future__ import annotations

import argparse
import os
import sys

from mailflow.auth import (
    GMAIL_READONLY,
    get_gmail_refresh_token,
    load_env_file,
    upsert_env_var,
)

GMAIL_BASE_URL = "https://gmail.googleapis.com/gmail/v1"
GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mailflow", description="mailflow CLI")
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth", help="obtain provider credentials")
    auth_sub = auth.add_subparsers(dest="provider")
    g = auth_sub.add_parser("gmail", help="get a Gmail refresh token via OAuth consent")
    g.add_argument("--client-id", default=None, help="OAuth client id (else env/.env/prompt)")
    g.add_argument("--client-secret", default=None, help="OAuth client secret (else env/.env/prompt)")
    g.add_argument("--env-file", default=".env", help="path to write the token (default .env)")
    g.add_argument("--port", type=int, default=8080, help="local redirect port (match the OAuth client)")

    check = sub.add_parser("check", help="verify saved credentials")
    check_sub = check.add_subparsers(dest="provider")
    cg = check_sub.add_parser("gmail", help="verify Gmail read + Pub/Sub access")
    cg.add_argument("--env-file", default=".env", help="path to read credentials from")

    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(flag: str | None, env_key: str, env_file: dict[str, str], prompt: str) -> str:
    """flag > process env > .env file > interactive prompt."""
    if flag:
        return str(flag)
    if os.environ.get(env_key):
        return os.environ[env_key]
    if env_file.get(env_key):
        return env_file[env_key]
    return input(prompt).strip()


def _gmail_client(client_id: str, client_secret: str, refresh_token: str) -> object:
    """Build a real GmailClient (lazy import — needs the `gmail` extra)."""
    from mailflow.adapters.gmail.client import GmailClient
    from mailflow.adapters.gmail.live import HttpxTransport, OAuthTokenProvider

    token = OAuthTokenProvider(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        token_uri=GMAIL_TOKEN_URI,
        scopes=[GMAIL_READONLY],
    )
    return GmailClient(base_url=GMAIL_BASE_URL, token_provider=token, transport=HttpxTransport())


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_auth_gmail(args: argparse.Namespace) -> int:
    env_path = str(args.env_file)
    env_file = load_env_file(env_path)

    client_id = _resolve(args.client_id, "GMAIL_CLIENT_ID", env_file, "Client ID: ")
    client_secret = _resolve(args.client_secret, "GMAIL_CLIENT_SECRET", env_file, "Client secret: ")
    if not client_id or not client_secret:
        print("error: a client id and secret are required", file=sys.stderr)
        return 2

    print("Opening your browser to authorize Gmail read access — sign in and click Allow…")
    try:
        token = get_gmail_refresh_token(client_id, client_secret, port=int(args.port))
    except ModuleNotFoundError:
        print("error: install the gmail extra first:  pip install \"mailflow[gmail]\"",
              file=sys.stderr)
        return 1
    if not token:
        print("error: no refresh token returned. Revoke prior access at "
              "myaccount.google.com -> Security and retry.", file=sys.stderr)
        return 1

    existed = upsert_env_var("GMAIL_REFRESH_TOKEN", token, path=env_path)
    print(f"OK  refresh token {'updated in' if existed else 'written to'} {env_path} "
          f"(length {len(token)}).")

    # Best-effort live confirmation so the user SEES it works.
    mbx_raw = os.environ.get("GMAIL_MAILBOXES") or env_file.get("GMAIL_MAILBOXES") or "me"
    mbx = (mbx_raw.split(",")[0].strip() or "me")
    try:
        client = _gmail_client(client_id, client_secret, token)
        prof = client.get_profile(mbx)  # type: ignore[attr-defined]
        print(f"OK  connected as {mbx}: {prof.get('messagesTotal')} messages, "
              f"historyId={prof.get('historyId')}")
    except Exception as exc:  # noqa: BLE001 - confirmation only; token is already saved
        print(f"(live check skipped: {type(exc).__name__}: {exc})")

    print("\nDone. Set GMAIL_MAILBOXES + PUBSUB_* in .env, then: connect(\"gmail\").")
    return 0


def cmd_check_gmail(args: argparse.Namespace) -> int:
    env_path = str(args.env_file)
    env_file = load_env_file(env_path)

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or env_file.get(key) or default

    client_id, secret = get("GMAIL_CLIENT_ID"), get("GMAIL_CLIENT_SECRET")
    refresh = get("GMAIL_REFRESH_TOKEN")
    mailboxes = [m.strip() for m in get("GMAIL_MAILBOXES", "me").split(",") if m.strip()]
    if not (client_id and secret and refresh):
        print("error: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN must be set "
              f"(run `mailflow auth gmail` first or check {env_path}).", file=sys.stderr)
        return 2

    ok = True
    try:
        client = _gmail_client(client_id, secret, refresh)
        for mbx in mailboxes:
            prof = client.get_profile(mbx)  # type: ignore[attr-defined]
            print(f"OK  Gmail read {mbx}: historyId={prof.get('historyId')} "
                  f"messages={prof.get('messagesTotal')}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"FAIL Gmail read: {type(exc).__name__}: {exc}")

    project, subscription = get("PUBSUB_PROJECT_ID"), get("PUBSUB_SUBSCRIPTION")
    if project and subscription:
        creds = get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds
        try:
            from google.cloud import pubsub_v1  # lazy: gmail extra

            sub = pubsub_v1.SubscriberClient()
            path = sub.subscription_path(project, subscription)
            resp = sub.pull(
                request={"subscription": path, "max_messages": 1, "return_immediately": True},
                timeout=15,
            )
            print(f"OK  Pub/Sub consume {path} (pulled {len(resp.received_messages)} pending, "
                  "left unacked)")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"FAIL Pub/Sub access: {type(exc).__name__}: {exc}")
    else:
        print("(skipped Pub/Sub check: PUBSUB_PROJECT_ID / PUBSUB_SUBSCRIPTION not set)")

    print("\n" + ("ALL CHECKS PASSED." if ok else "SOME CHECKS FAILED — fix the above."))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "auth" and getattr(args, "provider", None) == "gmail":
        return cmd_auth_gmail(args)
    if args.command == "check" and getattr(args, "provider", None) == "gmail":
        return cmd_check_gmail(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
