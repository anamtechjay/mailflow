# mailflow — Complete Testing Guide

> **The single source of truth for how mailflow is tested.** For every functionality it lists
> the test cases that cover it and how the edge cases are exercised.
>
> - **718 automated tests** — `python -m pytest` → *713 pass, 4 slow (deselected in CI), 1 documented xfail*.
> - **100% offline** — no mailbox, no cloud, no network. In-memory stores + fake transports.
> - **Tools:** `pytest` (behavior) + `hypothesis` (property/fuzz). No browser — mailflow is a headless library.
> - **Types:** `python -m mypy` — strict, clean across 87 source files.

---

## Table of contents
1. [How the automation works](#1-how-the-automation-works)
2. [The pipeline under test](#2-the-pipeline-under-test)
3. [Feature catalog F01–F17](#3-feature-catalog-f01f17)
4. [Reliability & scenario tests](#4-reliability--scenario-tests)
5. [Provider coverage (Gmail · Graph · Service Bus)](#5-provider-coverage)
6. [Cross-cutting functionality](#6-cross-cutting-functionality)
7. [How edge cases are tested](#7-how-edge-cases-are-tested)
8. [Coverage matrix & known gaps](#8-coverage-matrix--known-gaps)
9. [Quick reference — running subsets](#9-quick-reference)

---

## 1. How the automation works

**The machinery** (`tests/_harness/`):
- **`fakes.py`** — `_FakeGraphTransport` (canned Graph JSON), `_graph_message(...)` builder, `build_memory_pipeline`, `FaultExtractor` (injects failures on demand).
- **`email_builder.py`** — constructs RFC822 / Graph-JSON messages for a case.
- **`corpus.py`** — the real-email golden corpus (a 17-message Gmail freight thread), authored independently of the extractor so the two derivations must agree.
- **`reliability.py`** — crash / restart / multi-replica scaffolding.

**Principles**
- **No network, ever.** Providers are driven through fake transports and injected `poster`/clock functions; stores are in-memory or temp sqlite.
- **Ports + dependency injection.** Every seam (token provider, transport, cursor/dedupe/DLQ store, emitter, clock) is injectable, so behavior is tested without real infrastructure.
- **Behavior, not implementation.** Reliability is asserted on *dispositions* ("did it checkpoint?", "double delivery → one emit?"), not on mock call counts.
- **TDD.** Every case was written red → verified failing → minimal green.

**Markers** — `slow`, `reliability`, `live`. CI runs `-m "not slow"`.

---

## 2. The pipeline under test

```
claim → size-guard → parse → filter → classify → extract → emit → mark_done
   │                                                                    │
   └── 4 terminal dispositions advance a monotonic cursor ──────────────┘
        (emitted / dropped / duplicate / dead_lettered)
   transient failures are NON-terminal → released → redelivered
```

Every stage has a dedicated feature test (F0x) plus invariant tests that assert the
whole-spine guarantees (exactly-once effect, cursor monotonicity, fail-closed).

---

## 3. Feature catalog F01–F17

Each feature lives in `tests/qa/test_fNN_*.py`. Format: **what it guarantees · key cases · edge cases**.

### F01 — Dedupe / idempotency (`test_f01_dedupe.py`, 5)
- **Guarantees:** claim-before-spend; `idempotency_key = tenant|mailbox|provider_message_id`; at-least-once delivery → exactly-once *effect*.
- **Cases:** claim → record → mark_done → release lifecycle; a claimed key blocks a second claim; done key stays done.
- **Edge:** same id delivered 5× emits once (`test_scenarios_dup::test_same_id_x5_emits_once`); two streams with the same id counted independently.

### F02 — Size guard (`test_f02_size.py`, 4)
- **Guarantees:** oversized messages dead-lettered, not processed (fail-closed).
- **Edge:** size exactly at limit vs one over; size unknown → conservative handling (`test_pipeline_size_failclosed.py`).

### F03 — Parse / envelope (`test_f03_parse.py`, 6)
- **Guarantees:** RFC822 & Graph JSON → provider-neutral `Envelope`.
- **Edge:** missing headers, folded long Message-IDs, malformed dates → parsed without crash.

### F04 — Identity (`test_f04_identity.py`, 5)
- **Guarantees:** `canonical_id` = trusted `<..@..>` Message-ID, else deterministic `stable_hash`.
- **Edge:** absent Message-ID, malformed (no `<>`/`@`) → falls back to hash; trusted id used verbatim.

### F05 — Filter (`test_f05_filter.py`, 6)
- **Guarantees:** allow/deny/subject-regex filtering; `on_filtered=tag|drop` (default **tag**, still delivers).
- **Edge (`test_edge_cases.py`):** empty blacklist drops nothing; empty whitelist keeps nothing alone; address without `@`; case-insensitive domains; whitelist-keep short-circuits blacklist; first-drop-wins ordering.

### F06 — Classifier (`test_f06_classifier.py`, 3)
- **Guarantees:** auto-submitted & bounce detection from headers.
- **Edge:** ordinary mail not misclassified; classification flows parser → extractor intact.

### F07 — Extract (`test_f07_extract.py`, 5)
- **Guarantees:** `Envelope` + body → `CleanEmail`; **Gmail≡Graph parity**.
- **Edge:** HTML-only → readable `body_text`; multipart/alternative; **malformed base64 → PermanentError → DLQ (no retry)**; missing body → empty.

### F08 — Attachments (`test_f08_attachments.py`, 5)
- **Guarantees:** real vs inline classification, metadata, streaming to blob store, content-hash dedup.
- **Edge (`extract/test_attachment_safety.py`, `_policy.py`, `_streaming.py`):** path-traversal filenames sanitized (MIME-7), size cap / type allowlist policy modes, duplicate attachment bytes stored once.

### F09 — Stage orchestration (`test_f09_stages.py`, 3)
- **Guarantees:** custom stages run in order; a stage returning False drops.
- **Edge:** empty stage list passes through; `clean_fn` then stages ordering.

### F10 — Emit (`test_f10_emit.py`, 4)
- **Guarantees:** emitter receives `EmailEvent(schema_version, tenant, ordering_key, email)`.
- **Edge:** emit failure is non-terminal (redelivered), not a silent drop.

### F11 — Cursor (`test_f11_cursor.py`, 5)
- **Guarantees:** `commit_if_ahead` strictly monotonic; advances on **every** terminal disposition.
- **Edge:** out-of-order commit rejected (`order ≤ stored`); mid-batch failure doesn't advance past unread (`test_rel1_midbatch_cursor.py`).

### F12 — DLQ (`test_f12_dlq.py`, 3)
- **Guarantees:** poison/oversized → durable, replayable DLQ record; counted once via `add_dead_letter()`.
- **Edge:** DLQ store failure before `mark_done` leaves message reclaimable (idempotent re-dead-letter).

### F13 — Error taxonomy (`test_f13_errors.py`, 4)
- **Guarantees:** Permanent (poison→DLQ) vs Transient (retry) vs Auth (whole-stream) split.
- **Edge:** provider errors reparented under `MailflowError`, status codes preserved (`core/test_error_taxonomy.py`).

### F14 — State resolution (`test_f14_state.py`, 2)
- **Guarantees:** `state=` URI → cursor+dedupe store (memory / sqlite).
- **Edge:** unknown scheme raises; empty sqlite path raises; bare filename remapped (CFG-3).

### F15 — Providers / transport (`test_f15_providers.py`, 5)
- **Guarantees:** notification parse; Event Hubs ≡ Service Bus.
- **Edge:** **forged clientState dropped**; 404 graceful; 500 abandon+reraise; empty batch no-op.

### F17 — Fetch-by-id (`test_f17_fetch.py`, 3)
- **Guarantees:** `get_email/get_body/get_recipients/get_attachments` for a live provider.
- **Edge:** memory provider raises `NotImplementedError`; fetcher error propagates.

*(F16 intentionally unused — numbering follows the spec's feature list.)*

---

## 4. Reliability & scenario tests

The scenarios you asked about — **1000-email bulk, duplicates, network failure, server restart, multiple servers, DB crash** — each have dedicated tests.

| Scenario | Test | What it proves |
|---|---|---|
| **Crash mid-message** | `test_rel2_crash_midmessage.py` (3) | claim NOT reclaimable before lease; reclaimable after lease; done never reclaimable (REL-2 lease expiry) |
| **Retry exhaustion** | `test_rel3_retry_exhaustion.py` | after `max_attempts` → dead-letter, cursor advances |
| **Restart / reseed** | `test_rel5_restart_reseed.py`, `test_scenarios_restart.py` | resumes without re-emit; **kill+restart subprocess** re-reads state |
| **Checkpoint hold (network fail)** | `test_rel8_checkpoint_hold.py` (5) | non-terminal run → do NOT checkpoint (EH) / abandon (SB) → redelivery |
| **Multiple servers** | `test_dep2_multi_replica.py` (3) | two replicas claim each message once; shared dedupe → no double-emit; per-process store DOES double-emit (proves why shared store matters) |
| **Primary store crash** | `test_primary_store_crash.py` (2) | store failure surfaces safely, message reclaimable |
| **1000-email bulk** | `test_bulk_1k.py`, `test_scenarios_bulk.py` | throughput + all-terminal at scale |
| **Duplicates** | `test_scenarios_dup.py` (2) | same id ×5 → one emit; interleaved streams independent |
| **Large attachments** | `test_scenarios_large.py` | large payloads stream without OOM |
| **Multi-tenant** | `test_int2_multi_tenant.py` | tenant isolation in keys/cursors |
| **Soak** | `test_int7_soak.py` (slow) | sustained run stays terminal/monotone |
| **Harness self-tests** | `test_harness.py` (19), `test_harness_reliability.py` (22) | the test infrastructure itself is verified |

---

## 5. Provider coverage

### Gmail (Pub/Sub) — ~70 cases across `test_gmail_*.py`
- **Auth/token:** `test_gmail_token_rotation.py` (5), `test_gmail_rotation_sink.py` (5) — refresh-token rotation persisted & reloaded (SEC-4).
- **Client:** `test_gmail_client_errors.py` (5), `test_gmail_client_backoff.py` (5) — 429/5xx backoff, error mapping.
- **Watch lifecycle:** `test_gmail_watch_lifecycle.py` (9) — watch/renew/stop, stale-historyId self-heal.
- **Webhook:** `test_gmail_webhook.py` (9) — Pub/Sub push verification.
- **Provider/decode:** `test_gmail_provider_decode.py` (3), `test_gmail_fetch_isolation.py` (4) — poison message skipped, batch continues.
- **Threading:** `test_gmail_thread_key.py` (2) — `threadId → thread_key`.
- **E2E:** `test_gmail_e2e.py` (8), `test_gmail_reliability.py` (6).

### Microsoft Graph — 24 cases
- **Auth:** `test_graph_delegated_auth.py` (11) — authorize URL, **PKCE (S256)**, auth-code/refresh grants, token cache+expiry+error, **app-only client-credentials (no browser)**.
- **Extract/body:** `test_body_parity.py` (2), `test_classification_seam.py` (3).
- **Threading:** `test_thread_key.py` (3) — `conversationId → thread_key`, shared across a conversation, empty-fallback.
- **Fetch:** `test_f17_fetch.py` (3). **Notify/security:** `test_f15_providers.py` (5).

### Service Bus & transport parity — ~40 cases
- `providers/servicebus/` — config, eventgrid parse (8), emitter, runtime, e2e (16), live-adapter import guards.
- `test_transport_parity_e2e.py` (7) — **EH ≡ SB produce identical CleanEmail**; duplicate delivery deduped on both; forged clientState ignored.
- `test_connect_transport_selector.py` (5) — `delivery=eventhub|servicebus` selection, invalid → ValueError.

---

## 6. Cross-cutting functionality

| Area | Tests | Coverage |
|---|---|---|
| **Facade / `connect()`** | `test_facade*.py` (16), `test_connect_*.py` (17), `test_registry.py` (9) | provider/state/filter/emitter wiring; unknown provider raises |
| **Filters API** | `test_filters_api.py` (8), `test_facade_filters.py` (10), `test_to_cc_filters.py` (8), `test_filter_policy.py` (8) | allow/deny/regex, to/cc scoping, tag/drop policy |
| **Pipeline invariants** | `test_pipeline_invariants.py` (8), `test_pipeline_routing.py` (7), `test_pipeline_idempotency.py` | exactly-once, routing, fail-closed |
| **Observability** | `test_observability_edgecases.py` (13), `test_observers.py`, `test_transport_observers*.py`, `test_health.py` (7) | RunReport counts, traces, health/liveness (OBS-4) |
| **Logging** | `test_logging_setup.py` (4), `test_logging_edgecases.py` (8), `test_connect_logging.py` | structured logging sinks |
| **Security config** | `test_security_config.py` (8) | scope verification, allowlist wiring |
| **DLQ / redrive** | `test_redrive.py` (4), `test_pipeline_dlq_durable.py`, `stores/test_deadletter_stores.py` (4) | durable DLQ, replay preserving cursor+thread_key |
| **Stores** | `test_sqlite_stores.py` (9), `stores/test_blob_dedup.py`, `persistence/` (14) | cursor/dedupe/blob backends, lease semantics |
| **CLI** | `test_cli.py` (14) | `auth`/`check` for gmail+graph, env resolution |
| **Config/versioning** | `test_config_version.py`, `test_models_contract.py` (6), `test_event_contract.py` | SCHEMA_VERSION, model aliases |

---

## 7. How edge cases are tested

Edge cases use four deliberate techniques:

1. **Property-based / fuzz** (`hypothesis`) — `test_property_invariants.py`:
   - `test_parser_never_crashes_on_random_bytes` — feeds random bytes; parser must never throw.
   - `test_exactly_once_and_cursor_monotone` — across generated delivery orders, emit-once + monotone cursor always hold.
2. **Fault injection** — `FaultExtractor` / fake transports raise `Permanent`/`Transient`/`Auth`/404/500 on cue; tests assert the *disposition* (DLQ vs retry vs abort).
3. **Fail-closed assertions** — size/attachment/parse failures must **drop or dead-letter**, never silently pass (`test_pipeline_*_failclosed.py`).
4. **Boundary enumeration** — `test_edge_cases.py` (19) and `test_extract_edge_cases.py` (43) walk explicit boundaries: empty filters, missing `@`, case sensitivity, nested vs flat params, unknown scheme/provider/kind → raises, empty stages, restart persistence.

**Representative edge cases proven:**
- Empty whitelist keeps nothing; empty blacklist drops nothing.
- Address without `@`, domain case-insensitivity, subject regex.
- Malformed base64 / random bytes → no crash (poison → DLQ).
- Crash before lease vs after lease (reclaim timing).
- Two replicas on a shared store → each message once; on separate stores → double-emit (proves the contract).
- Out-of-order cursor commit rejected.
- Forged `clientState` dropped (constant-time compare).

---

## 8. Coverage matrix & known gaps

| Functionality | Coverage | Verdict |
|---|---|---|
| Pipeline spine (F01–F14) | dedicated feature tests + invariants | ✅ strong |
| Gmail provider (auth/watch/fetch/thread/e2e) | ~70 cases | ✅ strong |
| Graph auth (delegated+PKCE, app-only) | 11 cases | ✅ strong |
| Graph extract / threading / fetch / notify | 13 cases | ✅ strong |
| Service Bus + transport parity | ~40 cases | ✅ strong |
| Reliability (crash/restart/multi-replica/bulk/dup) | dedicated scenarios | ✅ strong |
| Attachments (safety/policy/streaming/dedup) | ~30 cases | ✅ strong |
| Observability / logging / health | ~30 cases | ✅ strong |
| **Graph `delta_sweep` poll pagination** | — | ⚠️ **gap** — `@odata.nextLink` not directly unit-tested |
| **Graph subscription create/renew/404-recreate** | indirect | ⚠️ thin — no dedicated unit test |
| **Live push (real subscription→notification)** | manual only | ⚠️ needs Azure infra |

*Findings and their scope verdicts are logged separately in [`qa-findings.md`](qa-findings.md).*

### 8.1 Live verification, 2026-07-13

The unit suite is 100% offline (by design — see §1), so it cannot catch a bug that only
Microsoft Graph's real API surfaces. A live, manual check against the real
`techjaystest4@nsrecycle.com` mailbox found one:

- **Bug found & fixed:** `GraphClient.list_attachments` (`src/mailflow/adapters/graph/
  client.py`) requested `$select=...,contentId` on the attachments-list endpoint.
  `contentId` only exists on the `fileAttachment` subtype, not the base `attachment`
  type that endpoint returns — Graph rejected it with HTTP 400 on every real
  attachment fetch. Not caught by any unit test because `_FakeGraphTransport` never
  modeled this Graph validation rule. Fixed by dropping `contentId` from the select
  (`GraphExtractor._attachments` already tolerates its absence via `.get`).
  `tests/qa/test_f17_fetch.py` (3/3) re-confirmed green after the fix.
- **Live end-to-end proof:** the real `Pipeline` (not a fake provider) was run against
  10 live messages via `GraphProvider` + `GraphEnvelopeParser` + `GraphExtractor`,
  including several with 4 attachments each (PDF, inline PNGs, a forwarded `.eml`).
  `RunReport: fetched=10 emitted=10 dropped=0 duplicates=0 dead_lettered=0`. One message
  was a brand-new live-arriving email during the check; its `CleanEmail` (body text,
  attachment metadata, and the decoded attachment bytes) was verified correct.
- **Takeaway:** this is exactly the kind of gap fake-transport unit tests structurally
  can't catch — a periodic live smoke test against a real mailbox (not just CI) is worth
  keeping in the loop for the Graph adapter specifically.

Full write-up (business + technical) of both providers' live-verified flows is in
[`mailflow-final-report.md`](mailflow-final-report.md).

---

## 9. Quick reference

```bash
# Everything (CI profile — excludes slow)
python -m pytest -m "not slow"

# Full suite incl. slow
python -m pytest

# By area
python -m pytest tests/qa                    # feature + scenario catalog
python -m pytest tests/providers/graph       # Graph adapter
python -m pytest tests/test_graph_delegated_auth.py   # Graph OAuth
python -m pytest -m reliability              # crash/restart/multi-replica
python -m pytest -k thread                   # threading everywhere

# Types
python -m mypy
```

**Current status:** 718 collected · 713 pass · 4 slow (deselected in CI) · 1 documented xfail · mypy strict clean.
