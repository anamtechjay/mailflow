# Production-Readiness Test Plan — mailflow

> **For agentic workers:** Use superpowers:subagent-driven-development to execute this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax. Each scenario is a **failing reproducer →
> source fix → green regression** cycle (TDD), *not* a test-only addition.

**Goal:** Close the 68-scenario production-readiness gap the 2026-07-07 audit found — turn the
current unit/seam suite (happy-path, single-message) into one that reproduces and fixes every
silent-mail-loss, deployment, custom-provider, MIME, error-identification, and security scenario.

**Architecture:** For each scenario we (1) build the adversarial condition the happy-path harness
avoids (multi-message batches, crash-between-claim-and-done, restart, cold-start, expired lease,
foreign charset), (2) write a test that **fails red** against today's code, (3) fix the source, (4)
keep the test as a regression guard. Cross-linked to existing grounded findings in `qa-findings.md`.

**Tech Stack:** pytest + hypothesis (existing), the `tests/_harness/` helpers (extended here), no
new runtime deps. Persistent-store scenarios use the sqlite store; provider scenarios use fakes.

## Global Constraints

- **This plan changes `src/`** (reliability/robustness fixes), unlike the pure-test QA suite. The
  old CLAUDE.md "tests only / no core edits" rule does **not** apply here — but every `src/` change
  must keep the full suite green and `python -m mypy` clean.
- **TDD, non-negotiable:** the reproducer must fail for the stated reason *before* the fix.
- **Commit policy is the user's call.** Default this session = local-only, uncommitted. To make the
  suite reviewable/runnable by others it must eventually be committed (Gate 0).
- **No behavior change without a test that pins it.** Every fix ships with its red→green reproducer.
- **Cross-link:** each scenario names its `qa-findings.md` ID so findings and tests stay in sync.
- Test names below are the **exact** function names to create.
- Suite must stay green: base run `python -m pytest -q`; reliability tier `-m reliability`.

---

# PART A — Full scenario catalog (all 68, by cluster)

Legend — **Status:** ✅ covered · 🟡 partial · ❌ missing. **Sev:** 🔴 blocker · 🟠 high · 🟡 med · ⚪ low.
"Fix" empty = behavior is correct, scenario is a **coverage-only** gap (test, no source change).

## Cluster REL — Reliability / silent mail loss

| ID | Scenario | Status | Sev | Test to write | Fix (file:line) | Finding |
|---|---|---|---|---|---|---|
| REL-1 | Batch `[A,B]`: A 429s, B succeeds → B's cursor commit leapfrogs A → A lost | ❌ | 🔴 | `test_midbatch_transient_does_not_skip_earlier` | low-water-mark cursor: `pipeline.py:157-164` only advance to highest position with all-prior terminal | **C1** |
| REL-2 | Crash between `try_claim` and `mark_done` → restart sees `duplicate` → cursor skips → lost | ❌ | 🔴 | `test_crash_before_done_is_reprocessed` | persistent store `try_claim` honors lease expiry: `stores/memory.py:51` (+ sqlite store) | **C2** |
| REL-3 | Poison retried across runs never DLQs (`release` erases attempts) | ❌ | 🔴 | `test_poison_reaches_dlq_at_max_attempts` (default `max_attempts=3`, **not** 1) | `release()` preserves record+attempts, clears only claimed flag: `stores/memory.py:65-68` | **I5/P1** |
| REL-5 | Restart re-seeds cursor to current historyId → downtime mail skipped every restart | ❌ | 🔴 | `test_restart_does_not_reseed_existing_cursor` | guard bootstrap: seed only when no stored cursor — Gmail watch bootstrap in `adapters/gmail/live.py` | C12* |
| REL-8 | Event Hub `process_batch` checkpoints past a transiently-failed msg | ❌ | 🔴 | `test_eventhub_checkpoint_respects_lowwater` | checkpoint to low-water-mark only: `adapters/graph` EH `process_batch` | audit |
| REL-4 | Cursor monotonic CAS rejects lower order | ✅ | — | (covered `test_pipeline_invariants.py`) | — | — |
| REL-7 | Dedupe catches redelivery of a single message | ✅ | — | (covered) | — | — |

\*C12 is named in the audit; no matching row exists in `qa-findings.md` yet — add it when writing REL-5.

## Cluster DEP — Deployment topologies

| ID | Scenario | Status | Sev | Test / action | Fix / decision |
|---|---|---|---|---|---|
| DEP-1 | Gmail on Cloud Function w/ default `state='memory'` → empty every cold start | ❌ | 🟠 | `test_memory_state_on_serverless_rejected` | runtime **guard**: reject/ warn memory-state when serverless env detected (`facade.connect`) |
| DEP-2 | N replicas share one mailbox → per-process `InMemoryDedupeStore` double-emits | ❌ | 🔴 | `test_two_pipelines_shared_mailbox_double_emit` (proves the hazard) | **require** an atomic cross-process store (Firestore/Redis) for >1 replica; document |
| DEP-3 | `sqlite:///…` on ephemeral disk; `sqlite:///mf.db` resolves to FS root → crash at `connect()` | ❌ | 🟠 | `test_sqlite_url_resolves_relative` | path resolution fix in the sqlite store URL parse (**CFG-3**) |
| DEP-4/REL-6 | Graph subscription never renewed → M365 halts silently ~6 days, health green | ❌ | 🔴 | `test_renewal_fires_before_expiry` (injected clock) | wire a renewal timer calling `renew_all()`/`reconcile()`; integrate `run_service` | **G1** |
| DEP-5 | Hybrid Gmail+Graph in one service | ❌ | 🟠 | `test_hybrid_gmail_graph_wiring` | document + one integration test through `run_service` |
| DEP-7 | k8s SIGTERM mid-batch (== REL-2 lease gap + no drain) | ❌ | 🔴 | `test_sigterm_midbatch_no_loss` | fixed by REL-2 fix + graceful-drain hook |

## Cluster CUST — Custom / BYO providers

| ID | Scenario | Status | Sev | Test / action | Fix / decision |
|---|---|---|---|---|---|
| CUST-1 | A single arbitrary `.eml` from a non-Google/MS user | ❌ | 🟠 | `test_eml_entry_point_end_to_end` | add a documented `eml`/`file` provider entry point |
| CUST-2 | BYO IMAP/Exchange provider end-to-end | ❌ | 🟠 | `test_byo_provider_full_invariants` (cursor+dedupe+DLQ together) | ship a copyable BYO example; `connect('imap')` currently raises `ValueError` |
| CUST-4 | Custom provider multi-message batch (== REL-1 for any provider) | ❌ | 🔴 | `test_byo_provider_midbatch_no_skip` | fixed by REL-1 fix; this is the generality proof |
| CUST-5 | Custom provider raising native errors (socket timeout / no-mailbox / auth) | ❌ | 🟠 | `test_byo_native_error_mapping` | enforce an error-mapping contract; unmapped ≠ silent transient-forever |

## Cluster MIME — Corrupted / real-world MIME

| ID | Scenario | Status | Sev | Test to write | Fix (file:line) | Finding |
|---|---|---|---|---|---|---|
| MIME-2 | Bogus/legacy charset (`unknown-8bit`,`big5`) → uncaught `LookupError` → foreign mail lost | 🟡(xfail) | 🔴 | un-xfail `test_unknown_charset_does_not_crash` | wrap `get_content()` with `errors='replace'`: `extract/mime.py` `_walk_body` ~`:199/202` | **E-1** |
| MIME-3 | Inline-dispositioned text body (mutt/Mailman) → read as attachment → empty email | ❌ | 🔴 | `test_inline_text_body_is_body_not_attachment` | derive `is_inline` once, prefer text/plain body: `extract/mime.py` ~`:210/217` | **E-2** |
| MIME-4 | `message/rfc822` forwarded-as-attachment | ❌ | 🟠 | `test_forwarded_eml_attachment_handled` | recurse or treat as opaque attachment (no crash) |
| MIME-7 | Hostile filename (`../../etc/passwd`) / nested-multipart bomb | ❌ | 🟠 | `test_hostile_filename_sanitized`, `test_multipart_depth_bounded` | sanitize filename; bound recursion depth in `_walk_body` |
| MIME-1 | encoded-word / HTML-only / base64-QP happy variants | ✅ | — | covered (F07 + golden) | — |

## Cluster FIL — Filtration

| ID | Scenario | Status | Sev | Test to write | Fix |
|---|---|---|---|---|---|
| FIL-1..4 | case-insensitive blacklist, To/Cc-vs-From, chain short-circuit, tag-vs-drop | ✅ | — | covered (F05) | — |
| FIL-5 | A user filter that **raises** → generic transient → retry-forever | ❌ | 🟠 | `test_user_filter_raise_does_not_retry_forever` | contain filter exceptions → deterministic disposition (depends on REL-3) |
| FIL-6 | Classifier abstain path (uncertain→classify→stamp→emit) | ❌ | 🟠 | `test_classifier_abstain_stamps_and_emits` | coverage-only (F06 extends) |
| FIL-7 | Invalid / ReDoS regex in a user filter | ❌ | 🟠 | `test_regex_filter_validated`, `test_regex_no_redos_bound` | build-time regex validation + a match timeout/complexity bound |

## Cluster OBS — Error identification & observability

| ID | Scenario | Status | Sev | Test to write | Fix |
|---|---|---|---|---|---|
| OBS-1 | "unknown Exception = transient" pinned; no proof deterministic failure ever DLQs | 🟡 | 🟠 | `test_deterministic_failure_reaches_dlq` | after REL-3 fix, prove exhaustion→DLQ; reconsider default classification |
| OBS-3 | Gmail bare `continue` on 404/410 skips poison silently | ❌ | 🟠 | `test_provider_skip_is_observable` | emit a trace/metric on skip: Gmail provider fetch path (**C15**) |
| OBS-4 | `health()` green while ingestion halted | ❌ | 🟠 | `test_health_reports_stalled_ingestion` | add last-progress liveness to `health()` |
| OBS-6 | `mf.stream()` loop errors die in a daemon thread; blocks forever, no stop API | ❌ | 🟠 | `test_stream_surfaces_loop_error_and_stops` | surface thread errors + a stop API (**C6**) |

## Cluster SEC — Security

| ID | Scenario | Status | Sev | Test to write | Fix |
|---|---|---|---|---|---|
| SEC-1 | Gmail OIDC push: forged bearer/audience/issuer rejected | ✅ | — | covered (`test_gmail_webhook.py`) | — |
| SEC-3 | 401 refresh-and-retry bound | ✅ | — | covered (`test_gmail_token_rotation.py`) | — |
| SEC-2 | Graph webhook verifier (HMAC `clientState`) has **zero tests** | ❌ | 🟠 | `test_graph_webhook_forged_clientstate_rejected` | coverage-only — verifier exists in `notifications.py` |
| SEC-4 | `FileTokenRotationSink.load()` never called in `src/` → stale token after rotate+restart | ❌ | 🟠 | `test_rotated_token_reloaded_after_restart` | wire `.load()` at startup: `adapters/gmail/live.py` bootstrap (**C13**) |
| SEC-7 | DLQ persists full raw-email PII to plain sqlite, no retention/encryption | ❌ | 🟡 | `test_dlq_pii_retention_policy` | retention/redaction policy (design decision) |

## Cluster SCALE / INT — Scale, soak, multi-tenant

| ID | Scenario | Status | Sev | Test to write | Fix |
|---|---|---|---|---|---|
| INT-2 | Multi-tenant isolation in one process (no cross-tenant dedupe collision / poison stall) | ❌ | 🟠 | `test_two_tenants_isolated` | coverage — proves `(tenant,mailbox,pmid)` keys isolate |
| INT-7 | Weeks-long run: dedupe `done` records never expire → unbounded growth | ❌ | 🟠 | `test_done_records_expire` (soak, injected clock) | TTL sweep of done records: `stores/memory.py` / sqlite |
| SCALE-3 | Unbounded store growth (== INT-7) | ❌ | 🟡 | (same) | (same) |
| SCALE-4 | 10 Pub/Sub callback threads + sweep share unlocked state → "dict changed size" / double-emit | ❌ | 🟠 | `test_concurrent_callbacks_no_race` | lock shared state in the Pub/Sub consume loop |
| INT-4 | Restart resume (== REL-5) | ❌ | 🔴 | (REL-5) | (REL-5) |

## Cluster CFG — Config

| ID | Scenario | Status | Sev | Test | Fix |
|---|---|---|---|---|---|
| CFG-1 | Unknown store kind rejected | ✅ | — | covered | — |
| CFG-2 | memory `get_email` → NotImplementedError | ✅ | — | covered (F17) | — |
| CFG-3 | `sqlite:///mf.db` resolves to FS root → crash | ❌ | 🟠 | `test_sqlite_relative_url` (== DEP-3) | sqlite URL path resolution |

---

# PART B — Test infrastructure to build first (harness extensions)

These live in `tests/_harness/` and unlock the reproducers. Build in Phase 0.

- **B1 `batch_with_faults(seed, fail={id: Exc})`** — a provider/extractor that raises a chosen error
  on specific message ids within one `fetch()` batch. Unlocks REL-1, REL-8, CUST-4, FIL-5.
- **B2 `crash_between_claim_and_done(store, key)`** — claims via a **persistent** (sqlite) store,
  then abandons without `mark_done`; a second pipeline is built over the same store. Unlocks REL-2,
  DEP-7, SEC-4.
- **B3 `FakeClock`** — injectable monotonic time threaded into store lease + subscription renewal, so
  expiry is deterministic. Unlocks REL-2, DEP-4, INT-7.
- **B4 `cold_start()`** — build a fresh `connect(...)` with `state='memory'` twice to model a
  serverless cold start. Unlocks DEP-1, DEP-3.
- **B5 `MIME_ADVERSARIAL`** — corpus: unknown charset, inline-dispositioned body, forwarded `.eml`,
  hostile filename, deep multipart. Unlocks MIME-2/3/4/7.
- **B6 `StubProvider`** — a minimal in-test class implementing the Provider port end-to-end (fetch,
  cursor, message_size), plus a variant that raises native errors. Unlocks CUST-1/2/4/5.
- **B7 `two_tenant_seed` / `soak_seed(n, clock)`** — multi-tenant + long-run drivers. INT-2, INT-7.

---

# PART C — Phased execution (TDD, ordered by risk)

Each task: **write failing reproducer → run red (confirm reason) → fix src → run green → suite green + mypy.**

## PHASE 0 — Harness extensions (B1–B7)
- [ ] Build B1–B7 with self-tests under `tests/qa/test_harness_reliability.py`. No `src/` change.
      Gate: self-tests pass, full suite still green.

## PHASE 1 — The 6 silent-loss reproducers (🔴 highest value)
Order: **REL-3 → REL-5 → REL-1 → CUST-4 → REL-2/DEP-7 → REL-8.** REL-3 and REL-5 are ~1-line fixes.
- [ ] **REL-3** red: `test_poison_reaches_dlq_at_max_attempts` fails (retries forever). Fix
      `stores/memory.py` `release()` to preserve the record+attempts (clear only the claimed flag);
      thread the same shape into the sqlite store. Green + add finding row confirming I5/P1 resolved.
- [ ] **REL-5** red: `test_restart_does_not_reseed_existing_cursor`. Fix: guard the Gmail watch
      bootstrap to seed the cursor only when none is stored. Green.
- [ ] **REL-1** red: `test_midbatch_transient_does_not_skip_earlier`. Fix `pipeline.py:157-164` to a
      low-water-mark commit (advance only to the highest position with all prior terminal). Green.
- [ ] **CUST-4** red via B6 StubProvider: `test_byo_provider_midbatch_no_skip` — same assertion, any
      provider. Passes once REL-1 fixed (generality proof).
- [ ] **REL-2 / DEP-7** red via B2+B3: `test_crash_before_done_is_reprocessed` on the sqlite store.
      Fix: persistent `try_claim` succeeds when the existing claim's lease expired and not done. Green.
- [ ] **REL-8** red: `test_eventhub_checkpoint_respects_lowwater`. Fix EH `process_batch` to checkpoint
      the low-water-mark. Green.
- [ ] Phase gate: `-m reliability` all green; full suite green; mypy clean.

## PHASE 2 — Deployment fitness (guards + renewal)
- [ ] DEP-2 hazard test + require-atomic-store guard/doc.
- [ ] DEP-4/REL-6 renewal loop wired + `test_renewal_fires_before_expiry` (B3 clock).
- [ ] DEP-1/DEP-3/CFG-3 serverless guards + sqlite URL resolution fix.
- [ ] DEP-5 hybrid wiring integration test + doc.
- [ ] **Deliverable:** `docs/deployment-fitness-matrix.md` (topology → Supported / Needs-store / Rejected).

## PHASE 3 — Real-inbox MIME + custom-provider seam
- [ ] MIME-2 un-xfail + `errors='replace'` fix (E-1). MIME-3 inline-body fix (E-2).
- [ ] MIME-4 forwarded-eml; MIME-7 filename sanitize + depth bound.
- [ ] CUST-1 `.eml`/`file` entry point + doc; CUST-2 BYO end-to-end; CUST-5 error-mapping contract.

## PHASE 4 — Error identification & observability
- [ ] OBS-1 deterministic-failure→DLQ (post REL-3); OBS-3 observable provider skip.
- [ ] OBS-4 health liveness; OBS-6 stream error surfacing + stop API.
- [ ] FIL-5 filter-raise containment; FIL-6 classifier abstain; FIL-7 regex validation + ReDoS bound.

## PHASE 5 — Security
- [ ] SEC-2 Graph webhook forged-clientState test; SEC-4 token reload after restart (C13).
- [ ] SEC-7 DLQ PII retention/redaction policy + test.

## PHASE 6 — Scale & soak
- [ ] INT-2 multi-tenant isolation; INT-7/SCALE-3 done-record TTL sweep (B3 clock);
      SCALE-4 Pub/Sub callback locking.

---

# PART D — Deliverables & gates

1. **Gate 0 (do with Phase 1):** commit the QA + reliability suite so it is reviewable/runnable
   (user's commit call). Add `reliability`, `soak`, `security` pytest markers to `pyproject.toml`.
2. **Deployment-fitness matrix** (`docs/deployment-fitness-matrix.md`).
3. **`qa-findings.md` sync:** each reproducer updates its finding row (C1, I5/P1, C2, E-1, E-2, G1,
   C13, C15, C6) from "open/deferred" to "reproduced + fixed (test: …)".
4. **CI tiers:** commit = `-m "not slow and not reliability and not soak"`; PR = `+ reliability`;
   nightly = `+ slow + soak`; gated = `live`.

## Coverage target
Move the audit's **24% covered / 12 blockers / 23 high** to **0 blockers, 0 high** with a
red→green reproducer for each, and every remaining item explicitly Deferred-by-design (not silent).

---

## Self-review notes
- Every blocker row names a grounded `file:line` and an existing finding ID — no placeholders.
- Phase 1 fixes touch `src/` (stores, pipeline, adapters) — flagged in Global Constraints; the
  old "tests-only" rule is explicitly lifted for this plan.
- REL-2/DEP-7/INT-7 depend on B3 `FakeClock`; REL-1/REL-8/CUST-4 on B1; REL-2/SEC-4 on B2 — all built
  in Phase 0 before the phases that consume them.
