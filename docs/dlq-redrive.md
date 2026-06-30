# DLQ & Redrive Runbook

mailflow dead-letters a message when it can never succeed as-is (poison/oversized/invalid
base64 → `PermanentError`), when a bounded retry budget is exhausted (`TransientError`
after `max_attempts`), or when a 401 still fails after one forced refresh + one retry. Every
dead-letter does two things:

1. **Counts once** in the `RunReport` (`add_dead_letter()` — the frozen single-count path).
2. **Persists a durable, replayable `DeadLetterRecord`** in the wired `DeadLetterStore`
   (when one is configured), carrying the original raw RFC822 bytes (base64), provider ids,
   stream, failure reason + error class, attempt count, and the cursor it was at.

The cursor always advances past a dead-lettered message, so the stream is never blocked.

## Wiring a durable store

Core / zero-setup path (`build_from_config`):

```python
from mailflow.builder import build_from_config
from mailflow.stores.sqlite import SqliteDeadLetterStore

pipeline = build_from_config(cfg, overrides={"dlq_store": SqliteDeadLetterStore("mailflow.db")})
```

Live Gmail path (`run_service`):

```python
from mailflow.adapters.gmail.live import run_service
from mailflow.stores.sqlite import SqliteDeadLetterStore

run_service(..., dlq_store=SqliteDeadLetterStore("mailflow.db"))
```

`run_service` is keyword-only; pass `dlq_store=` alongside the other live arguments
(`gmail_cfg=`, `pubsub_cfg=`, `tenant=`, `emitter=`, …). Without a `dlq_store`, dead-letters
still count and still hit the `dlq_emitter` sink, but they are NOT replayable — wire a store
if you want redrive.

## Inspecting the DLQ

```python
from mailflow.stores.sqlite import SqliteDeadLetterStore

for rec in SqliteDeadLetterStore("mailflow.db").list_pending():
    print(rec.record_id, rec.error_class, rec.attempts, rec.reason)
```

Or directly: `sqlite3 mailflow.db "SELECT record_id, json_extract(payload,'$.error_class'), json_extract(payload,'$.reason') FROM dead_letters;"`

(The whole `DeadLetterRecord` is stored as a single JSON `payload` column per `record_id`;
`json_extract` reaches any field — `$.attempts`, `$.canonical_id`, `$.cursor_order`, etc.)

## Redrive procedure

**Fix the root cause first.** Redrive replays the exact original bytes; if the underlying
problem (bad credentials, a parser bug, a too-low size ceiling, a downstream outage) is not
resolved, the message will simply dead-letter again and stay in the store.

1. Identify the failure class from `error_class` / `reason`.
2. Remediate:
   - `OversizedMessageError` → raise `max_message_bytes` in config.
   - `SizeUnknownError` → fix the provider's size metadata.
   - `PermanentError` (invalid base64 / 403 / 404 / 410) → usually NOT redrivable; the
     message is genuinely poison. Redrive only after confirming the cause was transient
     tooling (e.g. a since-fixed extractor bug).
   - `AuthError` (persisted after refresh+retry) / `TransientError` → fix credentials or
     wait out the downstream outage, then redrive.
3. Run the redrive:

```python
from mailflow.core.pipeline import PipelineConfig
from mailflow.core.redrive import redrive
from mailflow.emit.pubsub import build_pubsub_emitter
from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.sqlite import SqliteDeadLetterStore

store = SqliteDeadLetterStore("mailflow.db")
emitter = build_pubsub_emitter(project_id="my-gcp-project", topic="mailflow-emails")
report = redrive(
    store=store,
    emitter=emitter,           # your PRODUCTION Emitter (here, the live Pub/Sub emitter)
    dlq_emitter=emitter,       # where re-failures go (use a dedicated DLQ topic in prod)
    blob_store=LocalBlobStore("attachments"),
    config=PipelineConfig(tenant="acme"),
    limit=None,                # or an int to batch
)
print(report.examined, report.resubmitted, report.still_dead_lettered)
```

`redrive()` is keyword-only. It rebuilds each record into its original `RawMessage` and runs
it through a **fresh, redrive-scoped pipeline** (a fresh `DedupeStore` so the original
marked-done claim does not reject the replay, and a throwaway cursor store) using your
**real** emitter. A record that now reaches a non-DLQ terminal disposition
(emitted/dropped/duplicate) is **deleted** from the store; one that fails again is **kept**
for a later attempt.

**At-least-once:** the re-emitted event carries the original `idempotency_key`
`(tenant, mailbox, provider_message_id)`, so any consumer that already saw a partially
delivered copy dedupes it (§A4). Redrive is safe to re-run.

## Auth failure flow (401 / 403)

- **401 → `AuthError`:** the pipeline forces exactly ONE token refresh
  (`AuthRefresher.force_refresh()`), retries the message exactly once, and dead-letters if
  it still fails — it never loops on `max_attempts`. On the Gmail fetch surface the same
  refresh-once is applied inside `GmailClient` (one refresh + one retry per request).
- **403 → `PermanentError`:** dead-letters immediately, no retry (a 403 is a durable
  permission problem; redrive only after the grant/scope is fixed).
