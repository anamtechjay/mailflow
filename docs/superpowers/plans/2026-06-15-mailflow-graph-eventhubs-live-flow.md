---
date: 2026-06-15
topic: mailflow Graph + Event Hubs — live flow (the missing connection code)
status: done (code); live smoke test pending operator Azure provisioning
depends_on:
  - docs/superpowers/plans/2026-06-11-mailflow-graph-eventhubs-adapter.md
  - docs/superpowers/plans/2026-06-11-microsoft-graph-eventhubs-provisioning.md
---

# mailflow Graph + Event Hubs — live flow implementation plan

> **For agentic workers:** TDD, one behavior per cycle. Run the suite with
> `python -m pytest --import-mode=importlib` (this host has no `.venv`; the
> `CLAUDE.md` venv path is from another machine). Keep the unit suite **import-light**:
> the real Azure/MSAL/httpx SDKs are imported **locally inside `live.py` functions**
> only — no module-level vendor imports anywhere the unit suite touches.

## Overview

The Graph + Event Hubs adapter's *processing* logic is complete and unit-tested
(Tasks 0–9 of the 2026-06-11 adapter plan; 155 passing). What is missing is the
**live connection**: nothing actually connects to Azure Event Hubs, receives an
event, and drives `GraphEventHubsRuntime.process_batch`. `live.py::run_consume_loop`
is a `NotImplementedError` stub; there is no checkpointer adapter and no composition
root. This plan implements the live flow end-to-end plus the reliability hardening,
leaving only credential/key **values** for the operator to supply.

Decisions locked (this session):
- **Sync** consume loop (`EventHubConsumerClient` + sync `BlobCheckpointStore`) —
  matches the synchronous pipeline/runtime; swap the declared `…-aio` dep for sync.
- **Auth = pluggable**: `run_consume_loop` accepts a connection string OR an injected
  credential; the operator wires the actual secret/key.
- Azure SDK imports stay **local to `live.py`**.

## Success Criteria

- [ ] Unit suite stays green **offline** (no `azure`/`msal`/`httpx` installed):
      `python -m pytest --import-mode=importlib` passes.
- [ ] `mypy --strict` clean over `mailflow` (if mypy available on host).
- [ ] After Phase 1 + operator provisioning, a test email to a watched mailbox yields
      exactly one emitted `CleanEmail` end-to-end (manual smoke test, runbook Phase 7).
- [ ] Lifecycle events (`reauthorizationRequired`/`subscriptionRemoved`/`missed`) are
      handled, not dropped.
- [ ] Subscriptions are created + renewed from code (no hand-curl needed for steady state).
- [ ] Sent-items notifications carry `folder="sentitems"`, not a hardcoded `inbox`.
- [ ] A delta sweep recovers missed/після-restart messages idempotently.
- [ ] Docs updated: how to install the extra, set env, run the service.

## Current State

- `src/mailflow/adapters/graph/runtime.py` — `GraphEventHubsRuntime.process_batch(events)`;
  `EventHubEvent`/`Checkpointer`/`PipelineLike` Protocols. Checkpointer set once at init.
- `src/mailflow/adapters/graph/live.py` — `MsalTokenProvider`, `HttpxTransport` (real,
  local imports); `run_consume_loop` = `raise NotImplementedError`.
- `src/mailflow/adapters/graph/provider.py` — `GraphProvider`; `_stream_for` hardcodes
  `folder="inbox"`; synthetic counter `Cursor`.
- `src/mailflow/adapters/graph/notifications.py` — `parse_notification_payload` handles
  message change notifications only (no `lifecycleEvent`).
- `src/mailflow/adapters/graph/subscriptions.py` — `GraphSubscriptionManager`
  (`ensure_watch`/`renew_watch`/recreate-on-404); `_stream_by_handle` registry.
- `src/mailflow/adapters/graph/client.py` — messages, attachments, 429 retry, sub CRUD.
- `pyproject.toml:14` — `graph` extra declares the **aio** checkpoint store (wrong for sync).
- No `SecretProvider` implementation exists; core port is `SecretProvider.get(ref)->str`.
- Memory stores/emitter at `src/mailflow/stores/memory.py`, `src/mailflow/emit/memory.py`.

## What We're NOT Doing

- Rich/encrypted notifications (config-only switch, deferred).
- Attachment blob streaming (metadata only, per the adapter plan).
- Gmail adapter.
- Executing the Azure provisioning runbook (operator runs it; needs the subscription).
- Async/aio pipeline.

## Architecture

### Checkpointer bridge (Task 2)
Azure's sync batch callback gives `(partition_context, events)`; checkpointing is
`partition_context.update_checkpoint(event)`. The runtime's `Checkpointer` is
`update(event)`. Bridge per-batch:

```python
class EventHubCheckpointer:           # adapter for runtime's Checkpointer Protocol
    def __init__(self, partition_context): ...   # holds the SDK partition_context
    def update(self, event) -> None:             # -> partition_context.update_checkpoint(event)
```
`process_batch` gains an optional `checkpointer` arg overriding the instance default
(keeps existing tests green; the loop passes a fresh per-batch checkpointer).

### Composition root (Task 4) — no heavy imports
```python
def build_graph_runtime(*, graph_cfg, eh_cfg, token_provider, transport,
                        emitter, dlq_emitter, cursor_store, dedupe_store,
                        blob_store, filters, classifier=None) -> GraphEventHubsRuntime:
    # GraphClient(transport, token_provider) -> GraphProvider
    # -> Pipeline(GraphEnvelopeParser, GraphExtractor, stores...)
    # -> GraphEventHubsRuntime(provider, pipeline, client_state)
```
Testable entirely with `FakeTransport`/`FakeToken` + memory stores.

### Live entrypoint (Task 5) — `live.py`, azure imports local
```python
def run_service(*, graph_cfg, eh_cfg, secret_provider, connection_string=None,
                credential=None, checkpoint_*):
    token_provider = MsalTokenProvider(secret=secret_provider.get(graph_cfg.client_secret_ref), ...)
    transport = HttpxTransport()
    runtime = build_graph_runtime(...)
    run_consume_loop(runtime=runtime, ...)   # constructs EventHubConsumerClient + BlobCheckpointStore

def _make_batch_handler(runtime, checkpointer_factory):   # testable, no azure types
    def on_batch(partition_context, events):
        runtime.process_batch(events, checkpointer=checkpointer_factory(partition_context))
    return on_batch
```

### Lifecycle (Task 6) / Reconciler (Task 7) / Folder (Task 8) / Delta (Task 9)
- `parse_lifecycle_payload(body)` → `[GraphLifecycleEvent]`; `GraphLifecycleHandler`
  dispatches to subscription manager / delta sweep. Core port untouched (adapter-internal).
- `SubscriptionReconciler(client, sub_manager, config, eh)`: `reconcile()`, `renew_all()`.
- Folder: provider resolves `subscriptionId -> StreamRef` via a registry passed in
  (from the reconciler/sub-manager) instead of hardcoding inbox.
- `GraphClient.messages_delta(user, folder, delta_token)`; provider persists real
  `@odata.deltaToken` as `Cursor.value`; periodic `delta_sweep(stream)`.

## Testing Strategy

- Framework: pytest, `--import-mode=importlib`. One test module per source module
  (mirrors existing `tests/adapters/graph/`).
- Unit tests use `FakeTransport`/`FakeToken` + memory stores + fake partition context;
  **no azure import**. Batch-handler logic tested via `_make_batch_handler` with fakes.
- `run_consume_loop` body (the only azure-importing code) is validated by the manual
  smoke test (runbook Phase 7), not the unit suite.
- Quality gate: `mypy --strict` if available on host.

---

## Implementation Checklist

### Phase 1 — Make a real email flow (must-haves)

- [x] **Task 1: Sync deps**
  - Step: in `pyproject.toml`, replace `azure-eventhub-checkpointstoreblob-aio` with
    `azure-eventhub-checkpointstoreblob` in the `graph` extra; add `azure-storage-blob`.
  - Check: `python -m pytest --import-mode=importlib` still green (deps not imported by suite).

- [x] **Task 2: `EventHubCheckpointer` + per-batch checkpointer arg**
  - Step: add `EventHubCheckpointer` to `runtime.py` (or `live_support.py`); add optional
    `checkpointer` param to `GraphEventHubsRuntime.process_batch`.
  - Test: `tests/adapters/graph/test_checkpointer.py` — fake partition context records
    `update_checkpoint` calls; `process_batch(events, checkpointer=ehc)` checkpoints via it.
  - Check: new + existing runtime tests pass.

- [x] **Task 3: `EnvSecretProvider`**
  - Step: `src/mailflow/secrets/__init__.py` + `env.py` — `get("env://NAME")` → `os.environ`;
    pass-through for literals; implements core `SecretProvider`.
  - Test: `tests/secrets/test_env.py`.
  - Check: tests pass.

- [x] **Task 4: Composition root `build_graph_runtime`**
  - Step: `src/mailflow/adapters/graph/composition.py` — wire client→provider→pipeline→runtime;
    no azure/msal imports.
  - Test: `tests/adapters/graph/test_composition.py` — built runtime processes a fake
    notification batch to one emitted `CleanEmail`.
  - Check: tests pass.

- [x] **Task 5: `run_consume_loop` + `run_service` + testable batch handler**
  - Step: implement `_make_batch_handler` (no azure types) and `run_consume_loop`
    (local `from azure.eventhub import EventHubConsumerClient`; sync `BlobCheckpointStore`);
    `run_service` composition entrypoint.
  - Test: `tests/adapters/graph/test_live_handler.py` — `_make_batch_handler` drives
    `process_batch` with a per-batch checkpointer (fakes). `run_consume_loop` itself
    asserted to raise cleanly only on missing deps (no live assertion).
  - Check: tests pass; `import mailflow.adapters.graph.live` works WITHOUT azure installed.

### Phase 2 — Reliability hardening

- [x] **Task 6: Lifecycle handling**
  - Step: `parse_lifecycle_payload` in `notifications.py`; `GraphLifecycleHandler` routes
    reauthorizationRequired→reauthorize/renew, subscriptionRemoved→recreate, missed→delta sweep.
  - Test: `tests/adapters/graph/test_lifecycle.py` (one case per event) + runtime routes
    lifecycle events to the handler.
  - Check: tests pass.

- [x] **Task 7: `SubscriptionReconciler` + renewal**
  - Step: `src/mailflow/adapters/graph/reconciler.py` — `reconcile()` (desired = mailboxes×folders),
    `renew_all()`; thin scheduler hook in `live.py`.
  - Test: `tests/adapters/graph/test_reconciler.py` against `FakeTransport`.
  - Check: tests pass.

- [x] **Task 8: Folder-mapping fix**
  - Step: provider resolves folder via `subscriptionId → StreamRef` registry (from reconciler/
    sub-manager); remove hardcoded inbox; thread `subscription_id` through notification → provider.
  - Test: extend `test_provider.py` — inbox vs sentitems streams resolved correctly.
  - Check: tests pass.

### Phase 3 — Catch-up safety net

- [x] **Task 9: Real delta cursor + sweep**
  - Step: `GraphClient.messages_delta`; provider persists `@odata.deltaToken` as `Cursor`;
    `delta_sweep(stream)` pages `@odata.nextLink`; idempotent via dedupe.
  - Test: `tests/adapters/graph/test_delta.py` — paging, token persistence, idempotent replay.
  - Check: tests pass.

### Phase 4 — Docs

- [x] **Task 10: Docs**
  - Step: update `docs/graph-eventhubs-delivery.md` (live wiring section) + `README.md`
    (install `.[graph]`, env vars, `run_service`). Record deviations in Tracked Changes.
  - Check: docs render; commands accurate.

## Tracked Changes

- **All 10 tasks implemented 2026-06-15.** Suite: **179 passed** (was 155, +24);
  `mypy --strict`: clean (44→46 source files). Unit suite runs with **no azure/msal/
  httpx installed** (vendor imports local to `live.py`; mypy override ignores them).
- **Pre-existing `provider.py` type bug fixed** (Task-1 drive-by): the
  `internetMessageId` fallback reassigned `dict | None` to a `dict` var → split into a
  `fallback` local. This made `mypy --strict` fully clean for the first time on this branch.
- **`process_batch` gained an optional `checkpointer=` arg** instead of only the
  instance default — lets the consume loop inject a per-batch `EventHubCheckpointer`
  bound to that batch's `partition_context` (existing tests unaffected).
- **Shared `folder_resource()` helper** added to `subscriptions.py` and reused by the
  reconciler so manager and reconciler compute the resource path identically.
- **Folder fix uses the subscriptionId→stream registry** (not `parentFolderId`
  resolution as the connection doc speculated) — cheaper, no extra Graph call, since
  the reconciler already knows each subscription's stream.
- Lifecycle handling is **adapter-internal** (`GraphLifecycleHandler`), per the
  frozen-core-port recommendation in `graph-connection.md §D` — core port untouched.

## Status: COMPLETE (Phases 1–4)

## References

- Adapter plan: `docs/superpowers/plans/2026-06-11-mailflow-graph-eventhubs-adapter.md`
- Provisioning runbook: `docs/superpowers/plans/2026-06-11-microsoft-graph-eventhubs-provisioning.md`
- Delivery research: `docs/graph-eventhubs-delivery.md`
