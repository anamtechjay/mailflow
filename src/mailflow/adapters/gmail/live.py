"""Real SDK glue for the Gmail + Pub/Sub adapter. Every google/httpx import is LOCAL
to a function, so `import mailflow.adapters.gmail.live` works without the `gmail`
extra installed. Only calling run_service / run_consume_loop (or constructing the
providers) pulls in the SDKs.

Install the extra to run live:  pip install -e ".[gmail]"

Auth: per-user OAuth refresh token (works with a personal @gmail.com). The operator
supplies the client secret + refresh token via the SecretProvider; nothing hardcoded.
"""

from __future__ import annotations

from typing import Any, Callable

from mailflow.adapters.gmail.bootstrap import bootstrap_watches, renew_watches, sweep_once
from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.composition import build_gmail_runtime
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.runtime import GmailPubSubRuntime
from mailflow.adapters.gmail.scheduler import IntervalScheduler
from mailflow.adapters.gmail.watch import GmailWatchManager, WatchHandle
from mailflow.core.ports import (
    BlobStore,
    CursorStore,
    DedupeStore,
    Emitter,
    Filter,
    SecretProvider,
)

MessageCallback = Callable[[Any], None]


class OAuthTokenProvider:
    """App access token from a per-user OAuth refresh token (google-auth)."""

    def __init__(
        self, *, client_id: str, client_secret: str, refresh_token: str,
        token_uri: str, scopes: list[str],
    ) -> None:
        from google.oauth2.credentials import Credentials  # local import

        creds_cls: Any = Credentials  # route through Any: google-auth's __init__ is untyped
        self._creds: Any = creds_cls(
            token=None, refresh_token=refresh_token, token_uri=token_uri,
            client_id=client_id, client_secret=client_secret, scopes=scopes,
        )

    def get_token(self) -> str:
        from google.auth.transport.requests import Request  # local import

        request_cls: Any = Request
        if not self._creds.valid:
            self._creds.refresh(request_cls())
        return str(self._creds.token)


class HttpxTransport:
    """HttpTransport backed by httpx."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        import httpx  # local import

        self._client = httpx.Client(timeout=timeout)

    def request(self, method: str, url: str, *, headers: dict[str, str], json: Any | None) -> Any:
        return self._client.request(method, url, headers=headers, json=json)


def _make_message_callback(runtime: GmailPubSubRuntime) -> MessageCallback:
    """Build the Pub/Sub streaming-pull callback. Extracted (google-free) so its logic
    is unit-testable: each delivered message is run through the runtime (which acks
    after a successful pipeline run)."""

    def on_message(message: Any) -> None:
        runtime.process_messages([message])

    return on_message


def run_consume_loop(
    *,
    runtime: GmailPubSubRuntime,
    project_id: str,
    subscription: str,
    credentials: Any | None = None,
) -> None:
    """Blocking Pub/Sub streaming-pull loop. Uses Application Default Credentials when
    `credentials` is None. The runtime acks each message after a successful run, so a
    failed run is redelivered."""
    from google.cloud import pubsub_v1  # local import

    subscriber = (
        pubsub_v1.SubscriberClient(credentials=credentials)
        if credentials is not None
        else pubsub_v1.SubscriberClient()
    )
    sub_path = subscriber.subscription_path(project_id, subscription)
    future = subscriber.subscribe(sub_path, callback=_make_message_callback(runtime))
    with subscriber:
        try:
            future.result()
        except KeyboardInterrupt:
            future.cancel()
            future.result()


def run_service(
    *,
    gmail_cfg: GmailConfig,
    pubsub_cfg: PubSubConfig,
    tenant: str,
    secret_provider: SecretProvider,
    emitter: Emitter,
    dlq_emitter: Emitter,
    cursor_store: CursorStore,
    dedupe_store: DedupeStore,
    blob_store: BlobStore,
    credentials: Any | None = None,
    start_watch: bool = True,
    filters: list[Filter] | None = None,
) -> None:
    """Full live entrypoint: resolve OAuth secrets, build the token provider + httpx
    transport, start the Gmail watch (seeding the cursor), wire the runtime via the
    composition root, then run the consume loop."""
    token_provider = OAuthTokenProvider(
        client_id=gmail_cfg.client_id,
        client_secret=secret_provider.get(gmail_cfg.client_secret_ref),
        refresh_token=secret_provider.get(gmail_cfg.oauth_refresh_token_ref),
        token_uri=gmail_cfg.token_uri,
        scopes=gmail_cfg.scopes,
    )
    transport = HttpxTransport()
    # one service-level client, reused by the watch manager + the sweep.
    client = GmailClient(
        base_url=gmail_cfg.base_url, token_provider=token_provider,
        transport=transport, max_retries=gmail_cfg.max_attempts,
    )
    watch_manager = GmailWatchManager(client=client, config=gmail_cfg, pubsub=pubsub_cfg)
    handles: list[WatchHandle] = []
    if start_watch:
        # register the mailbox->topic watch and seed the cursor with its historyId,
        # otherwise no notifications are ever published.
        handles = bootstrap_watches(
            watch_manager=watch_manager, cursor_store=cursor_store,
            tenant=tenant, mailboxes=gmail_cfg.mailboxes,
        )
    runtime = build_gmail_runtime(
        gmail_cfg=gmail_cfg, pubsub_cfg=pubsub_cfg, tenant=tenant,
        token_provider=token_provider, transport=transport,
        emitter=emitter, dlq_emitter=dlq_emitter,
        cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
        filters=filters,
    )

    # Reliability: renew the watch (else it expires ~7 days) + a safety-net sweep, both
    # on background daemon threads while run_consume_loop blocks the main thread.
    scheduler = IntervalScheduler()
    if start_watch and gmail_cfg.watch_renew_seconds > 0 and handles:
        scheduler.every(
            gmail_cfg.watch_renew_seconds,
            lambda: renew_watches(watch_manager=watch_manager, handles=handles),
            "gmail-watch-renew",
        )
    if gmail_cfg.sweep_seconds > 0:
        scheduler.every(
            gmail_cfg.sweep_seconds,
            lambda: sweep_once(runtime=runtime, client=client, mailboxes=gmail_cfg.mailboxes),
            "gmail-sweep",
        )
    try:
        run_consume_loop(
            runtime=runtime,
            project_id=pubsub_cfg.project_id,
            subscription=pubsub_cfg.subscription,
            credentials=credentials,
        )
    finally:
        scheduler.stop()
