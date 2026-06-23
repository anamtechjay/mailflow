"""connect() — the friendly front door over the engine.

The library's whole job is: connect to an inbox, hand back a `CleanEmail`, and let the
consuming app do whatever it wants with it (store it, feed an LLM, notify someone). The
app owns storage; the library only keeps a tiny cursor/dedupe bookkeeping — in memory by
default (stateless; the app dedupes on `CleanEmail.canonical_id`), or on disk via
`state="sqlite:///mf.db"` for restart-safety.

Three ways to receive emails:

    mf = connect("gmail", credentials=creds, mailbox="me")
    for email in mf.stream(): ...           # pull loop

    mf = connect("gmail", credentials=creds, mailbox="me", on_email=handle)
    mf.run()                                # push to a callback (blocking)

    mf = connect("memory", seed=seed)
    emails = mf.fetch_new()                 # batch: one pass, return the list
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterator

from mailflow.config.state import resolve_state
from mailflow.core.models import CleanEmail, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.ports import CursorStore, Emitter, Filter
from mailflow.emit.callback import CallbackEmitter, QueueEmitter
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.registry import build_blob_store, build_cursor_store, build_dedupe_store
from mailflow.secrets import EnvSecretProvider

OnEmail = Callable[[CleanEmail], None]


class Mailflow:
    """Handle returned by connect(). Choose ONE consumption style per handle:
    `stream()` / `fetch_new()` (default queue output) or `run()` with an `on_email`
    callback."""

    def __init__(
        self,
        *,
        provider_kind: str,
        emitter: Emitter,
        cursor_store: CursorStore,
        run_once: Callable[[], object] | None = None,
        live_run: Callable[[], None] | None = None,
        queue: QueueEmitter | None = None,
    ) -> None:
        self.provider_kind = provider_kind
        self.cursor_store = cursor_store
        self._emitter = emitter
        self._run_once = run_once
        self._live_run = live_run
        self._queue = queue

    def fetch_new(self) -> list[CleanEmail]:
        """Run a single pass and return the emails it emitted. Batch/memory use."""
        if self._run_once is None:
            raise RuntimeError("fetch_new() is for the memory/batch provider")
        if self._queue is None:
            raise RuntimeError("fetch_new() requires the default queue output (omit on_email)")
        self._run_once()
        return self._queue.drain_nowait()

    def stream(self) -> Iterator[CleanEmail]:
        """Yield CleanEmails as they are emitted."""
        if self._queue is None:
            raise RuntimeError("stream() requires the default queue output (omit on_email)")
        if self._run_once is not None:  # memory: finite, drain after one pass
            self._run_once()
            yield from self._queue.drain_nowait()
            return
        if self._live_run is None:
            raise RuntimeError("nothing to stream")
        thread = threading.Thread(target=self._live_run, daemon=True)
        thread.start()
        yield from self._queue.items()  # blocks; live providers push indefinitely

    def run(self) -> None:
        """Blocking run. With an `on_email` callback this is the push loop; for memory
        it is a single pass."""
        if self._live_run is not None:
            self._live_run()
            return
        if self._run_once is not None:
            self._run_once()
            return
        raise RuntimeError("nothing to run")


def _pick_emitter(on_email: OnEmail | None) -> tuple[Emitter, QueueEmitter | None]:
    if on_email is not None:
        return CallbackEmitter(on_email), None
    q = QueueEmitter()
    return q, q


def connect(
    provider: str,
    *,
    credentials: dict[str, Any] | None = None,
    mailbox: str | None = None,
    seed: dict[StreamRef, list[SeedEmail]] | None = None,
    state: str = "memory",
    filters: list[Filter] | None = None,
    on_email: OnEmail | None = None,
    secret_provider: Any | None = None,
    tenant: str = "default",
) -> Mailflow:
    """Wire a runnable Mailflow for the given provider. `provider` is "memory" | "gmail"
    (graph is wired but not exposed here yet)."""
    stores = resolve_state(state)
    cursor_store = build_cursor_store(stores.cursor.kind, stores.cursor.params)
    dedupe_store = build_dedupe_store(stores.dedupe.kind, stores.dedupe.params)
    blob_store = build_blob_store(stores.blob.kind, stores.blob.params)
    emitter, queue = _pick_emitter(on_email)

    if provider == "memory":
        pipeline = Pipeline(
            provider=MemoryProvider(seed=seed or {}),
            parser=MimeEnvelopeParser(),
            filters=FilterChain(filters or []),
            extractor=MimeExtractor(),
            emitter=emitter,
            dlq_emitter=MemoryEmitter(),
            cursor_store=cursor_store,
            dedupe_store=dedupe_store,
            blob_store=blob_store,
            config=PipelineConfig(tenant=tenant),
        )
        return Mailflow(
            provider_kind="memory", emitter=emitter, cursor_store=cursor_store,
            run_once=pipeline.run_once, queue=queue,
        )

    if provider == "gmail":
        live = _build_gmail_live(
            credentials=credentials or {}, mailbox=mailbox, tenant=tenant,
            emitter=emitter, secret_provider=secret_provider or EnvSecretProvider(),
            cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
        )
        return Mailflow(
            provider_kind="gmail", emitter=emitter, cursor_store=cursor_store,
            live_run=live, queue=queue,
        )

    raise ValueError(f"unknown/unsupported provider {provider!r} (use 'memory' or 'gmail')")


def _build_gmail_live(
    *,
    credentials: dict[str, Any],
    mailbox: str | None,
    tenant: str,
    emitter: Emitter,
    secret_provider: Any,
    cursor_store: CursorStore,
    dedupe_store: Any,
    blob_store: Any,
) -> Callable[[], None]:
    """Return a blocking callable that runs the live Gmail consume loop. The Gmail SDK is
    imported lazily inside run_service, so importing this module needs no `gmail` extra."""
    from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
    from mailflow.adapters.gmail.live import run_service

    mailboxes = [mailbox] if mailbox else list(credentials.get("mailboxes", []))
    gmail_cfg = GmailConfig(
        client_id=str(credentials["client_id"]),
        client_secret_ref=str(credentials["client_secret_ref"]),
        oauth_refresh_token_ref=str(credentials["oauth_refresh_token_ref"]),
        mailboxes=mailboxes,
    )
    pubsub_cfg = PubSubConfig(
        project_id=str(credentials["project_id"]),
        topic=str(credentials["topic"]),
        subscription=str(credentials["subscription"]),
    )

    def live() -> None:
        run_service(
            gmail_cfg=gmail_cfg, pubsub_cfg=pubsub_cfg, tenant=tenant,
            secret_provider=secret_provider, emitter=emitter, dlq_emitter=MemoryEmitter(),
            cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
        )

    return live
