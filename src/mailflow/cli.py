"""mailflow command-line interface.

    mailflow auth gmail     one-time: browser OAuth -> save GMAIL_REFRESH_TOKEN to .env
    mailflow check gmail    verify the saved Gmail (+ Pub/Sub) credentials
    mailflow auth graph     one-time: Microsoft delegated OAuth -> save GRAPH_REFRESH_TOKEN
    mailflow check graph    verify the saved Graph token by reading /me/messages

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

    gr = auth_sub.add_parser("graph", help="get a Microsoft Graph refresh token via OAuth consent")
    gr.add_argument("--tenant", default=None, help="tenant id/domain, or 'common' (else env/.env/prompt)")
    gr.add_argument("--client-id", default=None, help="app (client) id (else env/.env/prompt)")
    gr.add_argument("--client-secret", default=None, help="client secret (else env/.env/prompt)")
    gr.add_argument("--env-file", default=".env", help="path to write the token (default .env)")
    gr.add_argument("--port", type=int, default=8765, help="local redirect port (match the app's redirect URI)")

    check = sub.add_parser("check", help="verify saved credentials")
    check_sub = check.add_subparsers(dest="provider")
    cg = check_sub.add_parser("gmail", help="verify Gmail read + Pub/Sub access")
    cg.add_argument("--env-file", default=".env", help="path to read credentials from")
    cgr = check_sub.add_parser("graph", help="verify Graph read (delegated /me, or --app-only /users/{mbx})")
    cgr.add_argument("--env-file", default=".env", help="path to read credentials from")
    cgr.add_argument("--app-only", action="store_true",
                     help="use the server-side client-credentials flow (no redirect/refresh token); "
                          "reads GRAPH_MAILBOXE via /users/{mailbox}/messages")

    rd = sub.add_parser("redrive", help="re-submit durable dead-letters through the pipeline")
    rd.add_argument("--state", required=True, help="state=URI, e.g. sqlite:///mf.db")
    rd.add_argument("--tenant", required=True, help="tenant name (must match the original run)")
    rd.add_argument("--limit", type=int, default=None, help="max records to examine")

    pg = sub.add_parser("purge", help="delete dedupe records past their done ttl")
    pg.add_argument("--state", required=True, help="state=URI, e.g. sqlite:///mf.db")

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
# Microsoft Graph (delegated) commands
# ---------------------------------------------------------------------------

def _resolve_multi(
    flag: str | None, env_keys: list[str], env_file: dict[str, str], prompt: str
) -> str:
    """flag > process env (any of env_keys) > .env file (any of env_keys) > prompt.
    Accepts several key spellings so both the documented GRAPH_* names and the shorter
    ones in this .env (Tenant_ID / App_Client_ID / SECRET_VALUE) work."""
    if flag:
        return str(flag)
    for k in env_keys:
        if os.environ.get(k):
            return os.environ[k]
    for k in env_keys:
        if env_file.get(k):
            return env_file[k]
    return input(prompt).strip()


def cmd_auth_graph(args: argparse.Namespace) -> int:
    from mailflow.adapters.graph.delegated_auth import get_graph_refresh_token

    env_path = str(args.env_file)
    env_file = load_env_file(env_path)

    tenant = _resolve_multi(args.tenant, ["GRAPH_TENANT_ID", "Tenant_ID", "TENANT_ID"], env_file, "Tenant id/domain: ")
    client_id = _resolve_multi(args.client_id, ["GRAPH_CLIENT_ID", "App_Client_ID", "APP_CLIENT_ID"], env_file, "Client id: ")
    client_secret = _resolve_multi(args.client_secret, ["GRAPH_CLIENT_SECRET", "SECRET_VALUE"], env_file, "Client secret: ")
    if not (tenant and client_id and client_secret):
        print("error: tenant, client id and client secret are required", file=sys.stderr)
        return 2

    print(f"Opening your browser to authorize Microsoft Graph mail read (tenant {tenant})…")
    print(f"  Sign in as the mailbox owner and click Consent. Redirect: http://localhost:{args.port}/")
    try:
        token = get_graph_refresh_token(
            tenant=tenant, client_id=client_id, client_secret=client_secret, port=int(args.port)
        )
    except Exception as exc:  # noqa: BLE001 - surface the real cause to the operator
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  Check: the app has a Web redirect URI http://localhost:"
              f"{args.port}/ and delegated Mail.Read + offline_access with consent granted.",
              file=sys.stderr)
        return 1

    existed = upsert_env_var("GRAPH_REFRESH_TOKEN", token, path=env_path)
    print(f"OK  refresh token {'updated in' if existed else 'written to'} {env_path} "
          f"(length {len(token)}).")
    print("\nDone. Verify it now with:  python -m mailflow check graph")
    return 0


def cmd_check_graph(args: argparse.Namespace) -> int:
    import json
    import urllib.request

    from mailflow.adapters.graph.delegated_auth import (
        AppOnlyGraphTokenProvider,
        DelegatedGraphTokenProvider,
    )

    env_path = str(args.env_file)
    env_file = load_env_file(env_path)

    def get(keys: list[str], default: str = "") -> str:
        for k in keys:
            if os.environ.get(k):
                return os.environ[k]
            if env_file.get(k):
                return env_file[k]
        return default

    tenant = get(["GRAPH_TENANT_ID", "Tenant_ID", "TENANT_ID"])
    client_id = get(["GRAPH_CLIENT_ID", "App_Client_ID", "APP_CLIENT_ID"])
    client_secret = get(["GRAPH_CLIENT_SECRET", "SECRET_VALUE"])

    tp: object
    if args.app_only:
        # Server-side: no redirect URI, no user, no refresh token. Reads a named mailbox.
        mailbox = get(["GRAPH_MAILBOXES", "GRAPH_MAILBOXE", "GRAPH_MAILBOX"]).split(",")[0].strip()
        if not (tenant and client_id and client_secret and mailbox):
            print("error: app-only needs Tenant_ID, App_Client_ID, SECRET_VALUE and "
                  f"GRAPH_MAILBOXE (the mailbox to read). Check {env_path}.", file=sys.stderr)
            return 2
        tp = AppOnlyGraphTokenProvider(
            tenant=tenant, client_id=client_id, client_secret=client_secret
        )
        query_url = (
            f"https://graph.microsoft.com/v1.0/users/{mailbox}/messages"
            "?$top=1&$select=subject,from,receivedDateTime"
        )
        print(f"(app-only / client-credentials — reading mailbox {mailbox})")
    else:
        refresh = get(["GRAPH_REFRESH_TOKEN"])
        if not (tenant and client_id and client_secret and refresh):
            print("error: delegated check needs Tenant_ID, App_Client_ID, SECRET_VALUE and "
                  f"GRAPH_REFRESH_TOKEN (run `mailflow auth graph` first) or use --app-only. "
                  f"Check {env_path}.", file=sys.stderr)
            return 2
        tp = DelegatedGraphTokenProvider(
            tenant=tenant, client_id=client_id, client_secret=client_secret, refresh_token=refresh
        )
        query_url = (
            "https://graph.microsoft.com/v1.0/me/messages"
            "?$top=1&$select=subject,from,receivedDateTime"
        )
    try:
        access = tp.get_token()
        req = urllib.request.Request(
            query_url, headers={"Authorization": f"Bearer {access}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed Graph endpoint
            data = json.loads(resp.read().decode("utf-8"))
        msgs = data.get("value", [])
        if not msgs:
            print("OK  authenticated as the user, but the inbox query returned 0 messages.")
        else:
            m = msgs[0]
            sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
            mode = "app-only" if args.app_only else "delegated"
            print(f"OK  Graph {mode} read works. Latest: {m.get('subject')!r} "
                  f"from {sender} at {m.get('receivedDateTime')}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL Graph read: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Redrive / purge (operational tooling over the durable stores)
# ---------------------------------------------------------------------------

def cmd_redrive(args: argparse.Namespace) -> int:
    from mailflow.config.state import resolve_state
    from mailflow.core.pipeline import PipelineConfig
    from mailflow.core.redrive import redrive
    from mailflow.emit.stdout import StdoutEmitter
    from mailflow.registry import build_blob_store, build_dead_letter_store

    try:
        stores = resolve_state(args.state)
    except (ValueError, NotImplementedError) as exc:
        print(f"error: invalid --state {args.state!r}: {exc}", file=sys.stderr)
        return 2
    dlq_store = build_dead_letter_store(stores.dead_letter.kind, stores.dead_letter.params)
    blob_store = build_blob_store(stores.blob.kind, stores.blob.params)

    report = redrive(
        store=dlq_store,
        emitter=StdoutEmitter(),
        dlq_emitter=StdoutEmitter(),
        blob_store=blob_store,
        config=PipelineConfig(tenant=args.tenant),
        limit=args.limit,
    )
    print(
        f"examined={report.examined} resubmitted={report.resubmitted} "
        f"still_dead_lettered={report.still_dead_lettered}"
    )
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    from mailflow.config.state import resolve_state
    from mailflow.registry import build_dedupe_store

    try:
        stores = resolve_state(args.state)
    except (ValueError, NotImplementedError) as exc:
        print(f"error: invalid --state {args.state!r}: {exc}", file=sys.stderr)
        return 2
    dedupe_store = build_dedupe_store(stores.dedupe.kind, stores.dedupe.params)
    purge = getattr(dedupe_store, "purge_expired", None)
    if purge is None:
        print(
            f"error: {type(dedupe_store).__name__} does not support purge_expired()",
            file=sys.stderr,
        )
        return 1
    removed = purge()
    print(f"purged {removed} expired dedupe record(s)")
    return 0


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
    if args.command == "auth" and getattr(args, "provider", None) == "graph":
        return cmd_auth_graph(args)
    if args.command == "check" and getattr(args, "provider", None) == "graph":
        return cmd_check_graph(args)
    if args.command == "redrive":
        return cmd_redrive(args)
    if args.command == "purge":
        return cmd_purge(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
