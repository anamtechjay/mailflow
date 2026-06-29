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
from typing import Any, Callable, Iterator, Mapping, TypeVar

from mailflow.config.state import resolve_state
from mailflow.core.models import Attachment, CleanEmail, Envelope, Recipient, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.ports import CursorStore, Emitter, Filter
from mailflow.emit.callback import CallbackEmitter, QueueEmitter
from mailflow.emit.memory import MemoryEmitter
from mailflow.emit.stages import Stage, StagesEmitter
from mailflow.extract.clean import ThinContentCleaner
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import FunctionFilter
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.registry import (
    build_blob_store,
    build_cursor_store,
    build_dedupe_store,
    build_filter,
)
from mailflow.secrets import EnvSecretProvider

# The callback receives a CleanEmail, or a projected dict when `fields` is set (spec §4a).
OnEmail = Callable[[Any], None]
# A filter input entry: a built-in spec, a user function, or a Filter object (spec §3.1).
FilterSpec = dict[str, Any] | Callable[[Envelope], bool] | Filter

_C = TypeVar("_C", bound=Callable[..., Any])


def as_filter(fn: _C) -> _C:
    """Tag a callable as a filter for the overrides= seam (A10). Returns it unchanged."""
    setattr(fn, "__mailflow_role__", "filter")
    return fn


def as_cleaner(fn: _C) -> _C:
    """Tag a callable as a cleaner for the overrides= seam (A10). Returns it unchanged."""
    setattr(fn, "__mailflow_role__", "cleaner")
    return fn


def make_projection(fields: list[str]) -> Callable[[CleanEmail], dict[str, Any]]:
    """Build a projection: email -> {field: value} for just the requested fields (spec §4a).
    Names are CleanEmail fields; "from" is accepted as an alias for `from_`. Unknown
    names raise ValueError. Empty list -> {}."""
    allowed = set(CleanEmail.model_fields)            # real field names (includes from_)
    plan: list[tuple[str, str]] = []                  # (output_key, attribute)
    for name in fields:
        attr = "from_" if name == "from" else name
        if attr not in allowed:
            raise ValueError(
                f"unknown field {name!r}; valid fields: {sorted(allowed | {'from'})}"
            )
        plan.append((name, attr))

    def project(email: CleanEmail) -> dict[str, Any]:
        return {key: getattr(email, attr) for key, attr in plan}

    return project


def normalize_filters(items: list[FilterSpec] | None) -> list[Filter]:
    """Turn the unified `filters=[...]` list into concrete Filters (spec §3.5):
    dict -> build_filter(kind, params); function -> FunctionFilter; Filter -> as-is."""
    out: list[Filter] = []
    for item in items or []:
        if isinstance(item, dict):
            out.append(build_filter(str(item["kind"]), item.get("params") or
                                    {k: v for k, v in item.items() if k != "kind"}))
        elif callable(item):
            out.append(FunctionFilter(item))
        else:
            out.append(item)
    return out


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
        fetcher: Callable[[str], CleanEmail] | None = None,
        project: Callable[[CleanEmail], dict[str, Any]] | None = None,
    ) -> None:
        self.provider_kind = provider_kind
        self.cursor_store = cursor_store
        self._emitter = emitter
        self._run_once = run_once
        self._live_run = live_run
        self._queue = queue
        self._fetcher = fetcher
        self._project = project

    def _deliver(self, email: CleanEmail) -> Any:
        """Project to the selected fields if `fields` was set (spec §4a), else the email."""
        return self._project(email) if self._project is not None else email

    # --- retrieve by message ID (spec §4): fetch parts on demand, no storage needed ---
    def get_email(self, message_id: str) -> CleanEmail:
        """Fetch the full message by its provider message ID and return a CleanEmail."""
        if self._fetcher is None:
            raise NotImplementedError(
                f"get_email is not supported for provider {self.provider_kind!r} "
                "(nothing to fetch); use it with a live provider like 'gmail'"
            )
        return self._fetcher(message_id)

    def get_body(self, message_id: str) -> str:
        return self.get_email(message_id).body_text

    def get_recipients(self, message_id: str) -> list[Recipient]:
        return self.get_email(message_id).to

    def get_attachments(self, message_id: str) -> list[Attachment]:
        return self.get_email(message_id).attachments

    def fetch_new(self) -> list[Any]:
        """Run a single pass and return the emitted emails (projected if `fields` set)."""
        if self._run_once is None:
            raise RuntimeError("fetch_new() is for the memory/batch provider")
        if self._queue is None:
            raise RuntimeError("fetch_new() requires the default queue output (omit on_email)")
        self._run_once()
        return [self._deliver(e) for e in self._queue.drain_nowait()]

    def stream(self) -> Iterator[Any]:
        """Yield each email as it is emitted (a CleanEmail, or a projected dict if `fields`
        was set)."""
        if self._queue is None:
            raise RuntimeError("stream() requires the default queue output (omit on_email)")
        if self._run_once is not None:  # memory: finite, drain after one pass
            self._run_once()
            for email in self._queue.drain_nowait():
                yield self._deliver(email)
            return
        if self._live_run is None:
            raise RuntimeError("nothing to stream")
        thread = threading.Thread(target=self._live_run, daemon=True)
        thread.start()
        for email in self._queue.items():  # blocks; live providers push indefinitely
            yield self._deliver(email)

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
    filters: list[FilterSpec] | None = None,
    fields: list[str] | None = None,
    stages: list[Stage] | None = None,
    clean_fn: Stage | None = None,
    on_email: OnEmail | None = None,
    secret_provider: Any | None = None,
    overrides: Mapping[str, Any] | None = None,
    tenant: str = "default",
) -> Mailflow:
    """Wire a runnable Mailflow for the given provider. `provider` is "memory" | "gmail"
    (graph is wired but not exposed here yet). `filters` is the unified list (spec §3);
    `fields` selects which data is delivered (spec §4a); `stages`/`clean_fn` post-process
    each CleanEmail (spec §5-6)."""
    stores = resolve_state(state)
    ov = overrides or {}
    # §A10: caller-supplied components replace the defaults for their role, before build.
    cursor_store = ov.get("cursor_store") or build_cursor_store(stores.cursor.kind, stores.cursor.params)
    dedupe_store = ov.get("dedupe_store") or build_dedupe_store(stores.dedupe.kind, stores.dedupe.params)
    blob_store = ov.get("blob_store") or build_blob_store(stores.blob.kind, stores.blob.params)
    cleaner = ov["cleaner"] if "cleaner" in ov else ThinContentCleaner()
    project = make_projection(fields) if fields is not None else None
    # if both fields + on_email are set, the callback receives the projected dict (§4a).
    if on_email is not None and project is not None:
        _user_cb = on_email
        _proj = project
        on_email = lambda email: _user_cb(_proj(email))  # noqa: E731
    emitter, queue = _pick_emitter(on_email)
    chain = normalize_filters(filters)          # specs/functions/objects -> Filters (§3.5)

    # clean_fn runs first, then stages; wrap the output emitter so the core pipeline is
    # untouched (spec §5). The Mailflow handle still drains the inner `queue`.
    all_stages = ([clean_fn] if clean_fn else []) + list(stages or [])
    pipe_emitter: Emitter = StagesEmitter(emitter, all_stages) if all_stages else emitter
    if "emitter" in ov:                         # §A10: caller replaces the sink entirely
        pipe_emitter = ov["emitter"]

    if provider == "memory":
        pipeline = Pipeline(
            provider=ov.get("provider") or MemoryProvider(seed=seed or {}),
            parser=MimeEnvelopeParser(),
            filters=FilterChain(chain),
            extractor=MimeExtractor(),
            emitter=pipe_emitter,
            dlq_emitter=MemoryEmitter(),
            cursor_store=cursor_store,
            dedupe_store=dedupe_store,
            blob_store=blob_store,
            cleaner=cleaner,
            config=PipelineConfig(tenant=tenant),
        )
        return Mailflow(
            provider_kind="memory", emitter=emitter, cursor_store=cursor_store,
            run_once=pipeline.run_once, queue=queue, project=project,
        )

    if provider == "gmail":
        live = _build_gmail_live(
            credentials=credentials or {}, mailbox=mailbox, tenant=tenant,
            emitter=pipe_emitter, secret_provider=secret_provider or EnvSecretProvider(),
            cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
            filters=chain,
        )
        fetcher = _build_gmail_fetcher(
            credentials=credentials or {}, mailbox=mailbox,
            secret_provider=secret_provider or EnvSecretProvider(), blob_store=blob_store,
        )
        return Mailflow(
            provider_kind="gmail", emitter=emitter, cursor_store=cursor_store,
            live_run=live, queue=queue, fetcher=fetcher, project=project,
        )

    raise ValueError(f"unknown/unsupported provider {provider!r} (use 'memory' or 'gmail')")


def _build_gmail_fetcher(
    *, credentials: dict[str, Any], mailbox: str | None, secret_provider: Any, blob_store: Any
) -> Callable[[str], CleanEmail]:
    """Build a fetch-by-message-id closure for Gmail (spec §4): messages.get(raw) ->
    MimeExtractor -> CleanEmail. SDK imports are lazy (inside this function)."""
    import base64

    from mailflow.adapters.gmail.client import GmailClient
    from mailflow.adapters.gmail.config import GmailConfig
    from mailflow.adapters.gmail.live import HttpxTransport, OAuthTokenProvider

    mailboxes = [mailbox] if mailbox else list(credentials.get("mailboxes", []))
    mbx = mailboxes[0] if mailboxes else "me"
    cfg = GmailConfig(
        client_id=str(credentials["client_id"]),
        client_secret_ref=str(credentials["client_secret_ref"]),
        oauth_refresh_token_ref=str(credentials["oauth_refresh_token_ref"]),
        mailboxes=[mbx],
    )
    token = OAuthTokenProvider(
        client_id=cfg.client_id, client_secret=secret_provider.get(cfg.client_secret_ref),
        refresh_token=secret_provider.get(cfg.oauth_refresh_token_ref),
        token_uri=cfg.token_uri, scopes=cfg.scopes,
    )
    client = GmailClient(base_url=cfg.base_url, token_provider=token, transport=HttpxTransport())
    extractor = MimeExtractor()

    def fetch(message_id: str) -> CleanEmail:
        data = client.get_message_raw(mbx, message_id)
        b64 = str(data.get("raw", ""))
        raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        return extractor.extract_bytes(
            raw, provider="gmail", provider_message_id=message_id,
            stream_id=mbx, watched_mailbox=mbx, blob_store=blob_store,
        )

    return fetch


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
    filters: list[Filter],
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
            filters=filters,
        )

    return live
