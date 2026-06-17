---
date: 2026-06-16
topic: mailflow Gmail adapter (Plan 3) — single library, two providers
status: done (code); live run pending operator GCP provisioning
spec: docs/superpowers/specs/2026-06-16-gmail-adapter-single-library-design.md
mirrors: docs/superpowers/plans/2026-06-11-mailflow-graph-eventhubs-adapter.md
---

# mailflow Gmail adapter — implementation plan (phase by phase)

> **Conventions (same as the Graph adapter):** strict TDD — failing test → confirm it
> fails for the stated reason → minimal impl → green → `mypy` clean → next. Run:
> `python -m pytest --import-mode=importlib` and `python -m mypy`. Keep the unit suite
> **import-light**: google SDKs (`google-auth`, `google-cloud-pubsub`,
> `google-api-python-client`) are imported **only locally inside `live.py`**. The core
> pipeline + §8 invariants + `MimeEnvelopeParser`/`MimeExtractor` are reused unchanged.

## Overview
Add Gmail as a peer provider behind `core/ports.py`, selected by `provider.kind="gmail"`.
Gmail's `messages.get(format='raw')` yields RFC822, so the adapter **reuses the core
MIME parser + extractor** — only transport/auth/provider/watch/runtime are new.

## Success Criteria
- [ ] `provider.kind="gmail"` builds a runnable stack via `build_from_config`.
- [ ] A mocked Pub/Sub event → one emitted `CleanEmail` (end-to-end test, fakes only).
- [ ] Offline unit suite green with **no google SDK installed**; `mypy --strict` clean.
- [ ] Gmail mock-email demo scripts run and print CleanEmail cards.
- [ ] Graph adapter + all existing tests untouched and still green.
- [ ] Docs: GCP provisioning runbook + README Gmail section.

## Current State
- Graph adapter at `src/mailflow/adapters/graph/` (config, client, notifications,
  provider, subscriptions, runtime, composition, live, testing) — the template.
- Reusable seams: `adapters/graph/transport.py` (`HttpResponse`/`HttpTransport`/
  `TokenProvider`/`GraphError`) — Gmail can import or mirror these.
- Core MIME path: `extract/envelope.py` (`MimeEnvelopeParser`),
  `extract/mime.py` (`MimeExtractor.extract_bytes(raw, *, provider, provider_message_id,
  stream_id, watched_mailbox)`) — the pipeline `_extract` already dispatches to it.
- `builder.py` raises `NotImplementedError` for non-memory providers; `registry.py`
  `PROVIDER_KINDS = {"memory"}`.

## What We're NOT Doing
Gmail labels in output; service-account DWD (config-only); attachment blob streaming;
GCP provisioning execution; async Pub/Sub.

## Architecture (snippets — patterns, not full code)

### Cursor = historyId
```python
Cursor(value=str(history_id), order=int(history_id))   # monotonic; commit_if_ahead-ready
```

### Provider fetch (reuses MIME path)
```python
def fetch(self, stream, cursor):
    start = cursor.value if cursor else None
    for msg_id in self.client.history_message_ids(stream.mailbox, start):   # history.list diff
        raw_b64 = self.client.get_message_raw(stream.mailbox, msg_id)       # messages.get(format=raw)
        yield RawMessage(provider="gmail", provider_message_id=msg_id, stream=stream,
                         raw_bytes=base64.urlsafe_b64decode(raw_b64), cursor=<historyId cursor>, ...)
# pipeline._extract sees a MimeExtractor -> extract_bytes(raw_bytes, provider="gmail", ...)
```

### Runtime (ack is the checkpoint)
```python
def process_messages(self, pubsub_messages):
    for m in pubsub_messages:
        addr, hid = parse_pubsub_message(m.data)
        self.provider.submit(hid); self.pipeline.run_once()
        m.ack()   # ack only after a successful run -> redelivery on failure
```

---

## Phase 0 — Scaffold + config
- [x] **Task 0.1: Package scaffold**
  - Steps: create `src/mailflow/adapters/gmail/__init__.py`, `tests/adapters/gmail/__init__.py`; test `test_package.py` imports the package.
  - Check: `pytest tests/adapters/gmail/test_package.py` passes.
- [x] **Task 0.2: Config models**
  - Steps: `config.py` — `GmailConfig` (oauth_refresh_token_ref, client_id, client_secret_ref, scopes default `["https://www.googleapis.com/auth/gmail.readonly"]`, mailboxes, label_ids default `["INBOX"]`, max_attempts); `PubSubConfig` (project_id, topic, subscription). pydantic validators (≥1 mailbox).
  - Test: defaults + validation. Check: `pytest tests/adapters/gmail/test_config.py`; `mypy`.

## Phase 1 — Core adapter logic (fakes only, no network)
- [x] **Task 1.1: Transport seam + fakes**
  - Steps: reuse `adapters/graph/transport.py` Protocols (`HttpTransport`, `TokenProvider`, `HttpResponse`) — import them; add `gmail/testing.py` `FakeGmailTransport` + `FakeToken` (FIFO queued responses, records calls). (If Graph's transport feels Graph-specific, lift the Protocols to `adapters/_http.py` and import in both — small, optional refactor.)
  - Test: fake serves queued responses + records calls. Check: `pytest`; `mypy`.
- [x] **Task 1.2: GmailClient**
  - Steps: `client.py` `GmailClient` over `HttpTransport`: `history_message_ids(user, start_history_id)` (`users.history.list`, page `historyId`, collect `messagesAdded[].message.id`), `get_message_raw(user, id)` (`messages.get?format=raw` → `raw`), `get_attachment`, `users.watch(topic,label_ids)`, `users.stop`; 429/`Retry-After` backoff; raise `GmailError`.
  - Test: each method builds correct URL + Bearer; 429-then-200 retry; 404 raises. Check: `pytest`; `mypy`.
- [x] **Task 1.3: Pub/Sub notification parse**
  - Steps: `notifications.py` `parse_pubsub_message(data: str|bytes) -> tuple[str,int]|None` — base64-decode → `{emailAddress, historyId}`; return None on malformed.
  - Test: valid → (addr, int historyId); malformed → None. Check: `pytest`.
- [x] **Task 1.4: GmailProvider (MailboxProvider)**
  - Steps: `provider.py` — `submit(history_id)`, `connect`, `sync_streams` (one `StreamRef(mailbox, folder=None)` per submitted/`config.mailboxes`), `fetch` (history diff → `get_message_raw` → `RawMessage` with RFC822 `raw_bytes` + historyId cursor), `message_size` (decode size or `sizeEstimate`). Full-resync path when cursor is None / on 404.
  - Test: fake transport with a history page + a raw message → yields a `RawMessage` whose `raw_bytes` decode to RFC822 and `cursor.value==historyId`. Check: `pytest`; `mypy`.
- [x] **Task 1.5: GmailWatchManager (SubscriptionManager)**
  - Steps: `watch.py` — `ensure_watch(stream)` (`users.watch` topic+label_ids → returns historyId/expiration), `renew_watch(handle)` (re-watch; daily cadence), recreate-on-404; `stop`.
  - Test: ensure_watch posts topic+labels; renew re-watches; 404 recreates. Check: `pytest`; `mypy`.
- [x] **Task 1.6: GmailPubSubRuntime**
  - Steps: `runtime.py` — `PubSubMessage`/`Acker` Protocols (duck-typed, no google import); `process_messages(msgs)`: parse → `provider.submit` → `pipeline.run_once()` → `m.ack()` after success (parallels GraphEventHubsRuntime + checkpoint).
  - Test: a fake Pub/Sub message drives the pipeline and gets acked; malformed msg acked without run. Check: `pytest`; `mypy`.
- [x] **Task 1.7: Composition root**
  - Steps: `composition.py` `build_gmail_runtime(*, gmail_cfg, pubsub_cfg, tenant, token_provider, transport, emitter, dlq_emitter, stores…, filters=None)` — wires `GmailClient`→`GmailProvider`→`Pipeline(parser=MimeEnvelopeParser(), extractor=MimeExtractor(), …)`→`GmailPubSubRuntime`. No google/vendor imports.
  - Test: built runtime processes a fake Pub/Sub batch → 1 emitted `CleanEmail` (via MimeExtractor). Check: `pytest`; `mypy`.
- [x] **Task 1.8: Mocked end-to-end**
  - Steps: `test_end_to_end.py` — Pub/Sub event (historyId) + fake transport (history page + raw RFC822) → emitted `CleanEmail` with expected subject/from. Check: `pytest`.

## Phase 2 — Library unification (builder / registry / config / public API)
- [x] **Task 2.1: Registry + builder wiring**
  - Steps: `registry.py` add `"gmail"` to `PROVIDER_KINDS`; `builder.py` `build_from_config`: when `cfg.provider.kind=="gmail"`, validate `cfg.provider.params` into `GmailConfig`/`PubSubConfig` and build the Gmail stack (token provider injected for tests). Keep `graph`/`memory` paths.
  - Test: `build_from_config` with a gmail config returns a runnable pipeline/runtime (fakes). Check: `pytest tests/test_builder.py`; `mypy`.
- [x] **Task 2.2: Curated public exports**
  - Steps: `adapters/gmail/__init__.py` exports `GmailClient, GmailConfig, PubSubConfig, GmailProvider, GmailWatchManager, GmailPubSubRuntime, parse_pubsub_message, build_gmail_runtime`.
  - Test: `test_package.py` imports them. Check: `pytest`.

## Phase 3 — Live SDK glue (`live.py`, google imports local)
- [x] **Task 3.1: Credentials + transport**
  - Steps: `live.py` `OAuthTokenProvider` (refresh-token → access token via `google.oauth2.credentials` / `google.auth.transport.requests`), `HttpxTransport` (or reuse Graph's), `run_service(...)` entrypoint composing the stack.
  - Test: `_make_message_handler` (azure-free analog) drives `process_messages` with fakes; `import adapters.gmail.live` works WITHOUT google SDK. Check: `pytest`.
- [x] **Task 3.2: Pub/Sub consume loop**
  - Steps: `run_consume_loop(*, runtime, project_id, subscription, credentials=None)` — local `from google.cloud import pubsub_v1`; `SubscriberClient().subscribe(sub_path, callback)` → `runtime.process_messages([msg])`; arg validation before SDK import. Add `pyproject` extra `gmail = ["google-auth", "google-api-python-client", "google-cloud-pubsub"]`; mypy override `ignore_missing_imports` for `google.*`.
  - Test: arg-validation raises offline; import works without SDK. Check: `pytest`; `mypy`.

## Phase 4 — Mock-email demos (mirror the Microsoft ones)
- [x] **Task 4.1: Gmail mock fixtures + runner**
  - Steps: `scripts/mock_gmail_messages.json` (raw RFC822 strings: inbound, reply w/ References, outbound, with-attachment, newsletter List-Id, auto-reply); `scripts/run_gmail_mock_emails.py` (feeds RFC822 via a mock provider → `MimeEnvelopeParser`+`MimeExtractor` → prints summary + writes CleanEmails) reusing `show_clean_emails.card`.
  - Check: `python scripts/run_gmail_mock_emails.py` prints emitted/dropped cards (manual QA).

## Phase 5 — Docs
- [x] **Task 5.1: GCP provisioning runbook**
  - Steps: `docs/google-setup-step-by-step.md` — GCP project, enable Gmail+Pub/Sub APIs, create topic+subscription, grant `gmail-api-push@system.gserviceaccount.com` Publisher, OAuth consent + client + refresh token, `users.watch`. Mirror the Azure runbook (incl. troubleshooting + "personal Gmail works via OAuth").
- [x] **Task 5.2: README + env template**
  - Steps: README "Live: Gmail via Pub/Sub" section; extend `.env.example` with `GMAIL_*`/`PUBSUB_*`.
  - Check: commands accurate.

## Tracked Changes
- **All phases implemented 2026-06-16.** Suite: **215 passed** (was 186, +29 Gmail);
  `mypy --strict` clean (58 source files). Offline — no google SDK required for the suite.
- **Phase 2 deviation:** `build_from_config` does **not** construct a live Gmail runtime
  (it returns a `Pipeline` and live providers need injected credentials/SDK). Instead,
  `registry.PROVIDER_KINDS` now recognizes `gmail`+`graph`, and `build_from_config`
  raises a helpful `NotImplementedError` pointing to `adapters.<kind>.live.run_service` /
  the composition root — matching the Graph adapter's precedent. Unification = shared
  core + per-provider composition roots with identical shape.
- **MimeExtractor reuse confirmed end-to-end:** Gmail emits `provider="gmail"`
  CleanEmails through the core `MimeEnvelopeParser` + `MimeExtractor` — no Gmail-specific
  parser/extractor written (the design's key payoff).
- **OAuth token call routed through `Any`** in `live.py` (google-auth's `Credentials`
  `__init__` is untyped) to keep `mypy --strict` clean with or without the SDK installed.
- **Watch renewal** = re-call `users.watch` (Gmail has no PATCH); `WatchHandle` carries
  mailbox + seed historyId. No recreate-on-404 special-case (not applicable like Graph's PATCH).

## Status: COMPLETE (Phases 0–5)

## References
- Spec: `docs/superpowers/specs/2026-06-16-gmail-adapter-single-library-design.md`
- Graph adapter (template): `docs/superpowers/plans/2026-06-11-mailflow-graph-eventhubs-adapter.md`
- Graph live flow: `docs/superpowers/plans/2026-06-15-mailflow-graph-eventhubs-live-flow.md`
- Spec §10 provider cheat sheet: `email-ingestion-toolkit-solution.md`
