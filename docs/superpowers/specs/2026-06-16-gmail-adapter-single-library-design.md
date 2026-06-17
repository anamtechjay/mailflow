---
date: 2026-06-16
topic: Gmail adapter — single library, two providers
status: approved
related:
  - docs/superpowers/plans/2026-06-11-mailflow-graph-eventhubs-adapter.md
  - docs/superpowers/plans/2026-06-15-mailflow-graph-eventhubs-live-flow.md
---

# Gmail adapter — single library, two providers (design)

## Overview

mailflow already ships a Microsoft Graph + Event Hubs adapter and an in-memory
provider. This adds **Gmail** as a *peer* provider in the **same library**, behind the
same `core/ports.py` interfaces, so a consuming app selects the provider purely by
config (`provider.kind = "graph" | "gmail" | "memory"`). The core pipeline and every
§8 correctness invariant are reused **unchanged**.

## Goals / Non-goals

**Goals**
- Gmail ingestion as a first-class adapter under `src/mailflow/adapters/gmail/`,
  mirroring the Graph adapter's module shape.
- Reuse the existing core **`MimeEnvelopeParser` + `MimeExtractor`** (Gmail's
  `messages.get(format='raw')` yields RFC822 → the proven MIME path).
- Per-user OAuth (works with a personal `@gmail.com`); Pub/Sub push delivery.
- Unify at `build_from_config`; unit-tested with fakes (zero network, no google SDK
  in the unit suite); mypy strict clean.

**Non-goals (YAGNI / deferred)**
- Gmail **labels in the emitted CleanEmail** (cost of reusing MimeExtractor — RFC822
  has no Gmail labels). Deferred to a follow-up.
- Service-account **domain-wide delegation** (config-only stub; OAuth first).
- Attachment **blob streaming** (metadata only, same as Graph).
- The GCP **provisioning** itself (separate operator runbook, like the Azure one).

## Architecture

### Unification point
Everything below the adapter line is shared and unchanged: `core/` (models, ports,
pipeline + §8 invariants, identity, filtering, events), `extract/`
(`MimeEnvelopeParser`, `MimeExtractor`), `stores/`, `emit/`, `config/`, `registry.py`,
`builder.py`. Providers are peers selected by `provider.kind`. No core changes.

### The reuse (Gmail needs no new parser/extractor)
`GmailProvider.fetch` fetches each changed message with `format='raw'`, base64url-
decodes it to RFC822 bytes, and puts them in `RawMessage.raw_bytes`. The pipeline's
existing extractor seam dispatches a `MimeExtractor` to `extract_bytes(raw, provider=
'gmail', …)`. So Gmail reuses **both** the core MIME parser and extractor — the adapter
is only transport/auth/provider/watch/runtime.

### New components — `src/mailflow/adapters/gmail/`
- `config.py` — `GmailConfig` (oauth refresh-token ref, scopes, `label_ids=['INBOX']`),
  `PubSubConfig` (project_id, topic, subscription).
- `transport.py` / reuse — injected `HttpTransport` + `TokenProvider` seam (same idea
  as Graph; may share Graph's `HttpResponse`/`HttpTransport` protocols).
- `client.py` — `GmailClient`: `history.list(startHistoryId)`, `messages.get(format=raw)`,
  `attachments.get`, `users.watch`, `users.stop`; 429 backoff.
- `notifications.py` — `parse_pubsub_message(envelope) -> (email_address, history_id)`
  (base64-decode the Pub/Sub `message.data`).
- `provider.py` — `GmailProvider` (MailboxProvider): `submit(history_id)`,
  `fetch(stream, cursor)` = `history.list(startHistoryId=cursor)` → message ids →
  `messages.get(raw)` → `RawMessage(raw_bytes=RFC822)`; `message_size` from `sizeEstimate`.
- `watch.py` — `GmailWatchManager` (SubscriptionManager): `ensure_watch` (`users.watch`
  topic + `label_ids`), `renew_watch` (daily), recreate-on-404.
- `runtime.py` — `GmailPubSubRuntime`: drains Pub/Sub messages → `provider.submit` →
  `pipeline.run_once()` → **ack** (ack is Gmail's checkpoint).
- `composition.py` — `build_gmail_runtime(...)` wiring credentials→client→provider→
  pipeline(**MimeExtractor**)→runtime.
- `live.py` — google SDK glue (`google-auth`, `google-cloud-pubsub`), imports **local**.
- `testing.py` — `FakeGmailTransport`, `FakeToken`.

### Data flow
new mail → `users.watch` → Pub/Sub gets `{emailAddress, historyId}` → runtime pulls →
`provider.submit(historyId)` → `run_once` → `history.list(cursor)` diff → `messages.get(raw)`
→ RFC822 → MimeParser → filter → MimeExtractor → emit → cursor=`historyId` → **ack**.

### Identity / cursor mapping
- `Cursor.value = historyId`, `Cursor.order = int(historyId)` (monotonic → fits
  `commit_if_ahead`). 404 ⇒ full resync.
- `idempotency_key = (tenant, mailbox, provider_message_id)` — identical to Graph.
- `StreamRef` = mailbox only (`folder=None`); one sync stream per mailbox.

### Builder wiring
`build_from_config`: `kind=='gmail'` → build the Gmail stack; `registry.PROVIDER_KINDS`
adds `'gmail'`; `provider.params` validates into `GmailConfig`/`PubSubConfig`.

## Testing strategy
One test module per source module under `tests/adapters/gmail/`, driven by
`FakeGmailTransport` + fakes — **no network, no google SDK** (vendor imports live only
in `live.py`). Plus a mocked end-to-end test (Pub/Sub event → emitted CleanEmail) and
Gmail mock-email demo scripts (`scripts/run_gmail_mock_emails.py` + RFC822 samples)
mirroring the Microsoft ones. `mypy --strict` clean; offline suite stays green.

## Key decisions
- **Reuse MimeExtractor** (approved) → labels deferred.
- **Per-user OAuth first** (approved) → personal Gmail works; DWD config-only later.
- **Parallel adapters unified at the builder** (approved) → minimal change to working
  Graph code; small duplication in the two runtimes accepted.
- **Sync Pub/Sub pull** to match the synchronous pipeline (as Event Hubs is sync).
