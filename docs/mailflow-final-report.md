# mailflow — Final Report (Business + Technical + Test Documentation)

_The single, current, top-level reference for mailflow: what it does, how each provider's
mail flows in, what's proven working, and how the whole thing is tested. Business framing
first in each section, technical detail underneath. This is deliberately the **one primary
document** for this project — a handful of specialized backing docs remain for detailed
provisioning/test/QA reference (linked inline), but everything that used to be spread
across a dozen overlapping write-ups now lives here._

**Verified as of 2026-07-14** against the live `techjaystest4@nsrecycle.com` (Graph) and
`jeevananthan.p@techjays.com` (Gmail) test mailboxes, plus the full automated test suite
(763 passed, including the production-hardening pass below).

---

## Table of contents
1. [Executive summary](#1-executive-summary)
2. [Feature inventory](#2-feature-inventory)
3. [How Gmail ingestion works](#3-how-gmail-ingestion-works)
4. [How Microsoft Graph ingestion works](#4-how-microsoft-graph-ingestion-works)
5. [Live delivery transports — Event Hubs & Service Bus](#5-live-delivery-transports--event-hubs--service-bus)
6. [What's proven vs. what needs setup](#6-whats-proven-vs-what-needs-setup)
7. [Automated test documentation](#7-automated-test-documentation)
8. [Current QA status & known issues](#8-current-qa-status--known-issues)
9. [Known gaps / next steps](#9-known-gaps--next-steps)
10. [Production hardening (2026-07-13/14)](#10-production-hardening-2026-07-1314)
11. [Quick reference — commands & credentials](#11-quick-reference--commands--credentials)
12. [Documentation map](#12-documentation-map)

---

## 1. Executive summary

mailflow is a **library, not a service**: one call, `connect(...)`, hands your application
a clean, normalized `CleanEmail` object for every message — sender, subject, body,
attachments, thread info — whether the mail lives in Gmail or Microsoft 365/Outlook.
**mailflow never stores your email**; it only remembers a tiny "how far have I read"
bookmark, so your application owns all the data.

**Business value:**
- One integration path for both Google Workspace and Microsoft 365 tenants.
- Mail can be filtered (block spam/personal domains, custom rules), tagged instead of
  silently dropped, and routed to your own storage/workflow.
- Two real-time delivery transports for Microsoft 365 (Azure Event Hubs and Azure Service
  Bus) are **already built and unit-tested** — turning either on is a credentials-only
  change, not a development task.
- Attachments (PDFs, invoices, images, forwarded `.eml` files) are captured with full
  metadata as part of every email.

**Proven working today, against real mailboxes (not just unit tests):**
- ✅ **Gmail** — full ingestion path, live-verified.
- ✅ **Microsoft Graph** — message read, extraction, and the real processing `Pipeline`,
  live-verified end-to-end including a 4-attachment message and a brand-new arriving
  email captured mid-session.
- ⚠️ Graph today only receives mail via a **manual/administrative pull** — always-on live
  delivery needs Event Hubs or Service Bus provisioned (§5), or a poll API to be built
  (§9).

**Automated test suite:** 763 passed (full suite, including slow tests and live Postgres
tests), 1 documented xfail, 0 failures; `mypy --strict` clean across 89 source files —
verified fresh today (§7). A 14-task production-hardening pass (§10) closed the gaps this
report used to list as open: TTL-bounded dedupe storage, durable dead-letter storage that
actually gets written to, operational CLI commands, Postgres resilience, and a minimal CI
workflow.

---

## 2. Feature inventory

| Feature | Status | Detail |
|---|---|---|
| Gmail ingestion (Pub/Sub push + poll fallback) | **Shipped, live-tested** | §3 |
| Microsoft Graph ingestion (message read + extraction) | **Shipped, live-tested** | §4 |
| Graph live delivery — Azure Event Hubs | Shipped, unit-tested | not yet run against real Event Hubs infra |
| Graph live delivery — Azure Service Bus (via Event Grid) | Shipped, unit-tested | not yet run against real Service Bus infra |
| One-line transport switch (`delivery="eventhub"\|"servicebus"`) | Shipped, tested | §5 |
| CleanEmail normalization (both providers → one shape) | Shipped, live-verified | identical fields regardless of provider |
| Attachment metadata extraction | Shipped, live-verified | filename, type, size, inline flag |
| Filtering (blacklist/allowlist/custom function) | Shipped | tag-by-default, drop optional |
| Dead-letter queue / retry handling | Shipped, unit-tested | poison messages quarantined, never block the stream |
| Cursor / dedupe (exactly-once over at-least-once feeds) | Shipped, unit-tested | |
| Fetch-by-message-ID (`get_email`/`get_body`/`get_attachments`) | Shipped | both providers |
| **Poll-on-demand for Graph** (`fetch_new()` without push infra) | **Not built** | §9 gap |
| Observability (per-message trace, run reports, health probes) | Shipped | |
| Postgres state (`state="postgresql://..."`) — restart-safe **and** multi-process-safe cursor/dedupe/DLQ | **Shipped, live-tested** (real local Postgres + disposable Docker Postgres) | `stores/postgres.py`; GCP Cloud SQL provisioning: `gcp-postgres-setup.md` |
| **TTL purge** — `mark_done(key, ttl_seconds)` now actually expires records; `purge_expired()` deletes them | **Shipped, tested** (all 3 stores: memory/sqlite/postgres) | §10; was previously accepted-and-silently-discarded — a real production bug |
| **Durable dead-letter storage wired into `connect()`** (memory + gmail) | **Shipped, tested** | §10; was previously always discarded into a throwaway in-memory emitter for *every* provider — the biggest gap this report used to flag |
| **`mailflow redrive` / `mailflow purge` CLI** | **Shipped, tested** | §10; surfaces `core/redrive.py` (already existed) and the new purge capability as operator commands |
| **Postgres connection retry/backoff** at store construction | **Shipped, tested** | §10; protects against a momentarily-unreachable server at process startup |
| **`state_ref=` secret-ref DSN resolution** | **Shipped, tested** | §10; a Postgres password never has to sit literally in code, same `env://` pattern as Gmail/Graph secrets |
| `ConfigError` (not raw `KeyError`) on missing Gmail/Graph credentials | **Shipped, tested** | §10 |
| `py.typed` marker, dependency version ceilings, minimal CI workflow | **Shipped** | §10; packaging/CI hygiene |

---

## 3. How Gmail ingestion works

**Business view:** Gmail asks Google's Pub/Sub to notify mailflow the instant new mail
arrives. mailflow reacts, asks Gmail exactly what changed, and hands back the new emails —
nothing is polled on a timer, so delivery is near-instant.

**Technical flow:**
1. **`watch()`** (`bootstrap_watches`) registers a Gmail push subscription to a Cloud
   Pub/Sub topic. Renewed periodically (7-day expiry) via `renew_watches` /
   `should_schedule_renew`.
2. A **Pub/Sub push message** carries only a `historyId` — a watermark, never trusted
   content (wake-signal-only).
3. **`history.list`** from the stored cursor to the new `historyId` returns changed
   message IDs (`GmailClient.history_message_ids`).
4. **`messages.get(format=raw)`** fetches full RFC822 bytes per changed message.
5. **`MimeEnvelopeParser`** does a cheap header-only parse first (for filtering, before
   spending on the body); **`MimeExtractor.extract_bytes`** does the full MIME parse into
   a `CleanEmail` (body text/html, attachments streamed to the `BlobStore`, thread key,
   bounce/auto-reply classification).
6. **`sweep_once`** is the catch-up path — a periodic re-check of `history.list` even
   without a push, so a missed/expired notification can't permanently lose mail.

**Live-verified this session:** `mailflow check gmail` passed; a direct Gmail search
(`from:jeevaskp1308@gmail.com`) found 201 total matching messages in
`jeevananthan.p@techjays.com`, several with attachments correctly extracted.

**Credentials:** `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`,
`GMAIL_MAILBOXES`, `PUBSUB_PROJECT_ID`, `PUBSUB_SUBSCRIPTION` — already configured and
working in this project's `.env`.

---

## 4. How Microsoft Graph ingestion works

**Business view:** conceptually the same as Gmail — a notification tells mailflow
something changed, mailflow asks Graph for the message, and extracts a clean structured
email. The gap today: the notification half (Event Hubs/Service Bus) isn't provisioned
against real Azure infrastructure yet, so mail is currently pulled by directly asking
Graph "what's new" rather than being pushed live.

**Technical flow (proven working):**
1. **`AppOnlyGraphTokenProvider`** (MSAL client-credentials, no user interaction) or
   **`DelegatedGraphTokenProvider`** (user sign-in) gets an access token.
2. **`GraphClient.get_message`** (or a direct `/users/{mailbox}/messages` list) fetches
   message JSON with the fields mailflow needs (`MESSAGE_SELECT`).
3. If `hasAttachments`, **`GraphClient.list_attachments`** fetches attachment metadata.
4. **`GraphEnvelopeParser.parse_envelope`** does the cheap pre-filter parse (from Graph's
   JSON + `internetMessageHeaders` — Graph never gives raw RFC822).
5. **`GraphExtractor.extract`** builds the full `CleanEmail` — same shape as Gmail's,
   including `conversationId → thread_key` parity and HTML→text fallback.
6. The same `Pipeline` runs claim → filter → extract → emit — identical correctness
   guarantees (dedupe, DLQ, cursor) regardless of provider.
7. **Live delivery (`GraphProvider`)** is notification-fed: `submit()` accepts a
   `GraphNotification` from an Event Hubs/Service Bus consume loop; `sweep()` runs a
   `/delta` catch-up query when a notification was missed. This piece needs real Azure
   infrastructure (§5) to run continuously.

**Live-verified this session, against `techjaystest4@nsrecycle.com`:**
- Direct message listing + sender/attachment filtering (found `techjaystest1@NSRecycle.com`
  mail with PDF/PNG/`.eml` attachments).
- The **real `Pipeline`** end-to-end: `RunReport: fetched=10 emitted=10 dropped=0
  duplicates=0 dead_lettered=0`, including a brand-new incoming email during the check
  (subject "13-08-2000"), whose full attachment content was downloaded and confirmed
  byte-correct.
- **A real bug found and fixed** (logged as **G1** in `qa-findings.md`): `GraphClient.
  list_attachments` requested `$select=...,contentId`, a field that doesn't exist on the
  base Graph `attachment` type — Graph 400'd on every attachment fetch, which would have
  crashed the live pipeline the moment any real attachment arrived. Fixed in
  `src/mailflow/adapters/graph/client.py`; `tests/qa/test_f17_fetch.py` (3/3) re-confirmed
  green.

**Credentials (app-only, works now):** `Tenant_ID`/`GRAPH_TENANT_ID`,
`App_Client_ID`/`GRAPH_CLIENT_ID`, `SECRET_VALUE`/`GRAPH_CLIENT_SECRET`,
`GRAPH_MAILBOXE`/`GRAPH_MAILBOXES` — already configured and working
(`mailflow check graph --app-only` passes).

---

## 5. Live delivery transports — Event Hubs & Service Bus

Both real-time delivery transports for Graph are **fully implemented and unit-tested
today**. `connect()` already exposes the switch:

```python
# Azure Event Hubs (default)
mf = connect("graph", delivery="eventhub", credentials={
    "tenant_id": "...", "client_id": "...", "client_secret_ref": "env://GRAPH_CLIENT_SECRET",
    "mailboxes": ["ops@acme.com"],
    "namespace": "evh-mailflow", "hub": "graph-notifications", "tenant_domain": "acme.com",
    "connection_string": "<eventhub-connection-string>",
})

# Azure Service Bus (via Event Grid partner topic)
mf = connect("graph", delivery="servicebus", credentials={
    "tenant_id": "...", "client_id": "...", "client_secret_ref": "env://GRAPH_CLIENT_SECRET",
    "mailboxes": ["ops@acme.com"],
    "fully_qualified_namespace": "<ns>.servicebus.windows.net", "entity_name": "mailflow-graph",
    "connection_string": "<servicebus-connection-string>",
})
```

`delivery` defaults to `"eventhub"` — nothing changes for existing code. Both paths build
the exact same `GraphProvider → GraphEnvelopeParser → GraphExtractor → Pipeline`; only the
notification transport differs.

**Verified this session:** `tests/providers/servicebus/` + `tests/test_connect_transport_
selector.py` → **41 passed**. Confirms wiring correctness at the unit level; **not yet run
against a real Azure Event Hubs or Service Bus resource.**

**To go live:** provision Azure infrastructure once (steps in
`docs/azure-servicebus-setup.md` for Service Bus; the Event Hubs equivalent is a namespace
+ hub + Graph subscription with an `EventHub:` notification URL), then supply the
resulting keys as `credentials=`. **No further code changes.**

| Transport | Extra to install | Credential keys |
|---|---|---|
| Event Hubs | `pip install -e ".[graph]"` | `namespace`, `hub`, `tenant_domain`, `connection_string` (or `credential=`) |
| Service Bus | `pip install -e ".[servicebus,graph]"` | `fully_qualified_namespace`, `entity_name`, `connection_string` (or `credential=`) |

---

## 6. What's proven vs. what needs setup

| Path | Code status | Infra status | Needs |
|---|---|---|---|
| Gmail push (Pub/Sub) | Shipped | **Live, working** | (already configured) |
| Graph manual pull (this session's live checks) | Proven live | **Live, working** | app-only credentials (already configured) |
| Graph live via Event Hubs | Shipped, unit-tested | Not provisioned | Event Hubs namespace + hub + Graph subscription |
| Graph live via Service Bus | Shipped, unit-tested | Not provisioned | Service Bus queue + Event Grid partner topic |
| Graph poll-on-demand (`fetch_new()`) | **Not built** | n/a | development work (§9) |

---

## 7. Automated test documentation

Full detail lives in **[`testing-guide.md`](testing-guide.md)**; this section is the
condensed reference.

**How it works:** 100% offline — no mailbox, no cloud, no network. Every seam (token
provider, transport, cursor/dedupe/DLQ store, emitter, clock) is dependency-injected, so
behavior is tested without real infrastructure. `pytest` for behavior, `hypothesis` for
property/fuzz tests. TDD throughout: every case written red → verified failing → minimal
green.

**Pipeline under test:**
```
claim → size-guard → parse → filter → classify → extract → emit → mark_done
   │                                                                    │
   └── 4 terminal dispositions advance a monotonic cursor ──────────────┘
        (emitted / dropped / duplicate / dead_lettered)
   transient failures are NON-terminal → released → redelivered
```

**Feature catalog (F01–F17)** — each functionality's guarantee, how it's tested, and its
edge cases:

| # | Functionality | Guarantees | How it's tested | Key edge cases |
|---|---|---|---|---|
| F01 | Dedupe/idempotency | claim-before-spend; at-least-once delivery → exactly-once effect | `test_f01_dedupe.py` (5) | same id ×5 emits once; independent streams |
| F02 | Size guard | oversized dead-lettered, fail-closed | `test_f02_size.py` (4) | exactly-at-limit vs one-over; unknown size |
| F03 | Parse/envelope | RFC822 & Graph JSON → neutral `Envelope` | `test_f03_parse.py` (6) | missing headers, folded Message-IDs, bad dates |
| F04 | Identity | `canonical_id` = trusted Message-ID else stable hash | `test_f04_identity.py` (5) | absent/malformed Message-ID → hash fallback |
| F05 | Filter | allow/deny/subject-regex; `on_filtered=tag\|drop` | `test_f05_filter.py` (6) | empty lists, no-`@` address, case-insensitivity, ordering |
| F06 | Classifier | auto-submitted & bounce detection | `test_f06_classifier.py` (3) | ordinary mail not misclassified |
| F07 | Extract | `Envelope`+body → `CleanEmail`; Gmail≡Graph parity | `test_f07_extract.py` (5) | HTML-only, multipart, bad base64 → DLQ, missing body |
| F08 | Attachments | real vs inline, metadata, streaming, hash-dedup | `test_f08_attachments.py` (5) | path-traversal filenames, size/type policy, dup bytes stored once |
| F09 | Stages | custom stages ordered; `False` drops | `test_f09_stages.py` (3) | empty stage list; clean_fn→stages ordering |
| F10 | Emit | emitter receives `EmailEvent` | `test_f10_emit.py` (4) | emit failure is non-terminal, redelivered |
| F11 | Cursor | `commit_if_ahead` strictly monotonic | `test_f11_cursor.py` (5) | out-of-order rejected; mid-batch failure doesn't skip ahead |
| F12 | DLQ | durable, replayable, counted once | `test_f12_dlq.py` (3) | store failure before mark_done stays reclaimable |
| F13 | Error taxonomy | Permanent/Transient/Auth split | `test_f13_errors.py` (4) | provider errors reparented under `MailflowError` |
| F14 | State resolution | `state=` URI → cursor+dedupe store | `test_f14_state.py` (2) | unknown scheme raises; bare filename remapped |
| F15 | Providers/transport | notification parse; EH ≡ SB | `test_f15_providers.py` (5) | forged clientState dropped; 404 graceful; 500 abandon+reraise |
| F17 | Fetch-by-id | `get_email`/`get_body`/`get_recipients`/`get_attachments` | `test_f17_fetch.py` (3) | memory provider raises `NotImplementedError` |

*(F16 intentionally unused — numbering follows the spec's feature list.)*

**Reliability & scenario tests:**

| Scenario | Proves |
|---|---|
| Crash mid-message | claim not reclaimable before lease; reclaimable after; done never reclaimable |
| Retry exhaustion | after `max_attempts` → dead-letter, cursor advances |
| Restart/reseed | resumes without re-emit, incl. kill+restart subprocess |
| Network-fail checkpoint hold | non-terminal run doesn't checkpoint/abandon → redelivery |
| Multiple servers | two replicas + shared dedupe → each message once; separate stores → double-emit (proves the contract) |
| Primary store crash | failure surfaces safely, message reclaimable |
| 1000-email bulk | throughput + all-terminal at scale |
| Duplicates | same id ×5 → one emit; interleaved streams independent |
| Large attachments | stream without OOM |
| Multi-tenant | tenant isolation in keys/cursors |

**Provider coverage:** Gmail ~70 cases (auth/token rotation, client backoff, watch
lifecycle, webhook, threading, e2e) · Graph 24 cases (delegated auth + PKCE, app-only,
extract/body parity, threading, fetch) · Service Bus & transport parity ~40 cases (EH ≡ SB
produce identical `CleanEmail`, forged clientState ignored, transport selector).

**Cross-cutting:** facade/`connect()` wiring, filters API, pipeline invariants,
observability, logging, security config, DLQ/redrive, store backends, CLI, config
versioning — see `testing-guide.md` §6 for the full table.

**How edge cases are tested (4 techniques):**
1. **Property-based/fuzz** (`hypothesis`) — parser never crashes on random bytes;
   exactly-once + monotone cursor hold across generated delivery orders.
2. **Fault injection** — fake transports raise Permanent/Transient/Auth/404/500 on cue;
   tests assert the *disposition*, not mock call counts.
3. **Fail-closed assertions** — size/attachment/parse failures must drop or dead-letter,
   never silently pass.
4. **Boundary enumeration** — 60+ explicit edge cases: empty filters, missing `@`, case
   sensitivity, unknown scheme/provider/kind → raises, empty stages, restart persistence.

**Current status (verified fresh today):**
```
python -m pytest -m "not slow"   →  713 passed, 4 deselected, 1 xfailed
python -m mypy                   →  Success: no issues found in 87 source files
```

---

## 8. Current QA status & known issues

Full running log: **[`qa-findings.md`](qa-findings.md)**. Summary of where things stand:

- **2026-06-09 Phase-1 review** — 2 actionable gaps found (C1 transient-retry path
  untested, I5 retry-attempt semantics unspecified). **Both resolved** (see 2026-07-07
  entry below).
- **2026-06-30 → 2026-07-07 phases** — attachment handling, config hardening/
  observability, subscription lifecycle, message classification/filtering, failure
  handling/recovery: reviewed, findings logged, hardening-level items noted as optional.
- **2026-07-07 QA automation build** — ~76 new tests (F01–F17 + scenarios + property
  tests) added; **I5 and its duplicate P1 (silent unbounded retry, poison message never
  reaching DLQ) fixed** — dedupe store `release()` now clears only the claim flag,
  keeping `attempts` as a lifetime counter across redeliveries.
- **2026-07-13 live Graph check (new, this session)** — **G1**: `list_attachments`
  `$select=contentId` bug, live-reproduced, fixed, re-verified with both the unit test and
  a live 10-message `Pipeline.run_once()` run. This is a class of bug offline fixtures
  structurally can't catch (Graph's real OData type-validation rules) — worth a
  periodic live smoke test against a real mailbox going forward, not just CI.

- **2026-07-13/14 production-hardening pass (§10)** — **I1**: `SqliteDedupeStore`'s
  migration path added `claimed_at`/`expires_at` to a legacy table but never `claimed`,
  which would crash every existing sqlite-state deployment on upgrade at the first
  `try_claim()`. Found by the pass's own final whole-branch review (not any single-task
  review), reproduced independently, fixed, re-verified independently. This is the kind
  of cross-task gap a broad review catches that per-task review structurally can't.

**No currently-open actionable findings** as of this report — all logged Phase gaps have
either been fixed or are explicitly deferred/hardening per their scope verdict in
`qa-findings.md`.

---

## 9. Known gaps / next steps

1. **No polling API for Graph.** Unlike Gmail (live push + periodic sweep fallback),
   `connect("graph", ...)` today only offers `get_email(id)` (single message) or
   `live_run` (requires Event Hubs/Service Bus). Everything verified in §4 was hand-
   assembled scripting, not a supported library entry point. Two ways forward: build
   `connect("graph", delivery="poll")` using the existing `GraphProvider.sweep()` `/delta`
   mechanism (no Azure infra needed), or go straight to provisioning Event Hubs/Service
   Bus (§5).
2. **`Graph delta_sweep` pagination** (`@odata.nextLink` following) has no dedicated unit
   test — logged in `testing-guide.md` §8 as a coverage gap.
3. **Graph subscription create/renew/404-recreate** has only indirect coverage.
4. **Live push against real Azure infrastructure** (Event Hubs or Service Bus) has not
   been exercised — only unit-tested with monkeypatched SDKs.
5. **Durable dead-letter storage is wired for `memory` + `gmail` only** (§10) — Graph
   (Event Hubs) and Service Bus were explicitly deferred (deliberate scope boundary, not
   an oversight) since that touches the transport-specific composition roots rather than
   just the facade. Follow-up: thread `dlq_store` through `adapters/graph/composition.py`
   /`live.py` the same way `adapters/gmail/composition.py` already does.
6. **No `mf.dlq_store` / `mf.list_dead_letters()` accessor** for `state="memory"` callers
   — the store is correctly populated now (§10), but there's no way to read it back
   in-process without going through the CLI against a persistent `state=`. Minor, noted by
   the final review, not actioned.

---

## 10. Production hardening (2026-07-13/14)

A full production-readiness review found 8 gaps; a 14-task plan closed them (Global
Constraint: Azure Service Bus/Event Hub *configuration* was explicitly out of scope —
this hardened the parts of the system that don't depend on provisioning real Azure infra).

**Phase 1 — Critical correctness:**
- `ConfigError` instead of a raw `KeyError` when `connect()`'s `credentials={...}` dict is
  missing a required key (gmail/graph) — the first thing an operator hits when
  misconfiguring a deploy.
- **TTL purge for real.** `DedupeStore.mark_done(key, ttl_seconds)` accepted `ttl_seconds`
  but silently discarded it in all three stores — the `claims`/dedupe table grew
  unbounded forever in any long-running deployment. Fixed in `InMemoryDedupeStore`,
  `SqliteDedupeStore`, and `PostgresDedupeStore`: `mark_done` now stamps a real
  `expires_at`, and a new `purge_expired(*, now=None) -> int` method deletes expired
  `done` records. Exposed on the facade too: `mf.purge_expired()`.

**Phase 2 — Durable dead-letter storage + redrive:**
- **The big one.** `connect()` never wired a durable `DeadLetterStore` for *any* provider
  — every dead-lettered message vanished into a throwaway `MemoryEmitter()` nobody could
  read back, regardless of `state=`. Fixed for `memory` and `gmail`: `StoresConfig` gained
  a `dead_letter` field (mirroring `cursor`/`dedupe`/`blob`), `resolve_state()` populates
  it, and `connect()` now builds a real store and threads it into the pipeline. (Graph
  Event Hubs and Service Bus are explicitly deferred — see §9.)
- **`mailflow redrive --state … --tenant … [--limit N]`** and **`mailflow purge --state
  …`** — new CLI subcommands surfacing `core/redrive.py` (already built, previously
  unused) and the new purge capability as operator commands, following the same argparse
  pattern as the existing `auth`/`check` commands. A review-driven fix added clean
  `error: ...` + exit-code-2 handling for a bad `--state` value (was a raw traceback).

**Phase 3 — Resilience & Postgres hardening:**
- **`connect_with_retry()`** — a small backoff helper wrapping the Postgres store
  constructors' initial connection, protecting against a momentarily-unreachable server
  at process startup (e.g. during a rolling restart).
- **`connect(state_ref=...)`** — resolves the `state=` DSN via the existing
  `SecretProvider` port at connect-time, the same `env://...` indirection Gmail/Graph
  secrets already use, so a Postgres password never has to sit literally in application
  code.

**Phase 4 — Packaging (light touch, by design):**
- `py.typed` (PEP 561 marker, so consumers get real type checking on the library),
  dependency version upper bounds in `pyproject.toml`, and a minimal GitHub Actions
  workflow (`python -m pytest -m "not slow"` + `python -m mypy` on every push/PR).
  License/CHANGELOG/Dockerfile were deliberately left out of this pass.

**Process:** executed via subagent-driven development — one fresh implementer + one
independent reviewer per task, strict TDD throughout, plus a final whole-branch review
across all 14 tasks together. That final review is what caught **I1** (§8) — a bug no
single task's review could have seen, since it spanned work from an earlier session
(the original lease-expiry migration) and this session's TTL-purge task.

**Verification:** 763 passed (full suite incl. slow + live Postgres), 1 xfailed, 0
failures; `mypy --strict` clean across 89 files.

---

## 11. Quick reference — commands & credentials

```bash
# Tests
python -m pytest -m "not slow"        # CI profile: 738 passed, 21 skipped (no Postgres DSN), 4 deselected, 1 xfailed
python -m pytest                      # full suite incl. slow: 763 passed, 1 xfailed (with Postgres DSN configured)
python -m pytest tests/qa             # feature + scenario catalog
python -m pytest tests/providers/graph
python -m mypy                        # strict, clean across 89 files

# CLI checks (live credentials, already configured in this project's .env)
python -m mailflow check gmail
python -m mailflow check graph --app-only

# Operational commands (new)
python -m mailflow redrive --state sqlite:///mailflow.db --tenant acme --limit 50
python -m mailflow purge --state sqlite:///mailflow.db
```

| Provider | Required credentials (this project's `.env`) |
|---|---|
| Gmail | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`, `GMAIL_MAILBOXES`, `PUBSUB_PROJECT_ID`, `PUBSUB_SUBSCRIPTION` |
| Graph (app-only, works now) | `Tenant_ID`/`GRAPH_TENANT_ID`, `App_Client_ID`/`GRAPH_CLIENT_ID`, `SECRET_VALUE`/`GRAPH_CLIENT_SECRET`, `GRAPH_MAILBOXE`/`GRAPH_MAILBOXES` |
| Graph + Event Hubs (not yet provisioned) | above, plus `namespace`, `hub`, `tenant_domain`, `connection_string` |
| Graph + Service Bus (not yet provisioned) | above, plus `fully_qualified_namespace`, `entity_name`, `connection_string` |
| Postgres state (local or GCP Cloud SQL) | `POSTGRES_DSN` (e.g. `postgresql://user:pass@host:5432/mailflow`) passed as `state=` or `state_ref="env://POSTGRES_DSN"` |

---

## 12. Documentation map

This report is the primary entry point. The docs tree was consolidated on 2026-07-14 —
a dozen overlapping status/spec/showcase write-ups were folded into this one report and
removed; what's left is a small set of documents with genuinely distinct purposes:

| Document | Purpose | Why it's separate from this report |
|---|---|---|
| [`README.md`](../README.md) | Repo entry point, quickstart | What GitHub/an IDE shows first |
| [`INSTALL.md`](../INSTALL.md) | Install from a distributed wheel | Different audience — someone who received a `.whl`, not a repo checkout |
| [`getting-started.md`](getting-started.md) | Hands-on "~15 lines" tutorial | Tutorial style/pacing, not reference style |
| [`testing-guide.md`](testing-guide.md) | Full test documentation, uncondensed | Too much detail to inline here without drowning the business framing |
| [`qa-findings.md`](qa-findings.md) | Append-only QA findings log, every review since 2026-06-09 | A running log, not a snapshot — must never be condensed/overwritten |
| [`dlq-redrive.md`](dlq-redrive.md) | DLQ/redrive runbook (wiring, auth-failure flow) | Deeper operational procedure than the CLI summary in §10 |
| [`azure-servicebus-setup.md`](azure-servicebus-setup.md) | Service Bus provisioning steps | Infra runbook, not conceptual documentation |
| [`gcp-postgres-setup.md`](gcp-postgres-setup.md) | GCP Cloud SQL provisioning steps | Infra runbook, not conceptual documentation |
| [`../examples/feature-explorer-v2.html`](../examples/feature-explorer-v2.html) | Interactive, self-contained "toggle a feature, see real code" demo | A living demo, not prose — covers every `connect()` kwarg including today's additions (Graph source, Postgres state, `state_ref`, observability hooks, `on_filtered`, `purge_expired`, and the CLI ops section) |
| `docs/superpowers/plans/*.md`, `.thoughts/plans/*.md` | Historical implementation plans | Frozen build records (CLAUDE.md: "the plan is the source of truth for what to build") — never edited after the fact, so they can't be folded into a living report |

**Removed on 2026-07-14** (superseded by this report, no longer needed): `mailflow-overview.md`,
`azure-setup-step-by-step.md`, `gmail-working-flow.md`, `gmail-devops-handoff.md`,
`graph-connection.md`, `graph-eventhubs-delivery.md`, `live-flow-qa.md`,
`mailflow-call-explanation.md`, `mailflow-complete-spec.md`, `mailflow-flow-diagram.md`,
`mailflow-full-spec.md`, `mailflow-showcase.md`, `mailflow-spec.md`,
`mailflow-status-for-manager.md`, `mailflow-usage-guide.md`, `project-qa.md`, and the root
`email-ingestion-toolkit-solution.md`.
