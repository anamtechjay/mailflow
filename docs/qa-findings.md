# mailflow — QA findings log

Running record of QA / test-coverage review findings. **Append to this file** every time a QA
pass turns up something; don't let findings live only in a chat transcript. Each finding gets an
ID, a severity, evidence (`file:line`), and a **scope verdict** against the plan-of-record so we
never confuse "the plan deliberately deferred this" with "we missed this."

**How to classify a finding** (this is the important part — see the 2026-06-09 review for why):
- **Phase gap** — code shipped in this phase but no test exercises it. *Actionable now.*
- **Deferred** — the plan explicitly pushed it to a later plan (2/3/4). *Not a gap; note and move on.*
- **Hardening** — the behavior is tested per plan; this is extra rigor beyond what the plan asked.
- **Withdrawn** — flagged on review, then found already covered or incorrect.

Always judge a coverage gap against **Phase scope** (`docs/superpowers/plans/…`), not the spec's
full ambition. The spec (`email-ingestion-toolkit-solution.md` §8) describes the end state across
4 plans; a given phase implements a deliberately narrowed slice.

---

## Review 2026-06-09 — Phase-1 test suite (core spine)

Reviewer: pr-test-analyzer (via `/pr-review-toolkit:review-pr`). Baseline: **79 passed in 0.25s**,
no flakes, no shared mutable state, no over-mocking. The suite matches the Phase-1 plan's specified
`def test_…` names essentially **1:1** — the engineers built exactly what Phase 1 asked.

### Actionable now — genuine Phase-1 gaps (code shipped, untested)

| ID | Sev | Finding | Evidence | Fix |
|----|-----|---------|----------|-----|
| **C1** | 🔴 Critical | Transient-retry path (`release` → cursor does **not** advance → reclaim next run) is never tested. Cycle E deliberately uses `max_attempts=1`, which goes straight to DLQ and skips the branch. A regression that advanced the cursor on transient failure would **silently drop mail** and the suite stays green. | code: `src/mailflow/core/pipeline.py:152-159`; test only at `tests/core/test_pipeline.py:105-119` (`max_attempts=1`) | Add a flaky extractor that raises on attempt 1, succeeds on attempt 2, with `max_attempts>=2`. After run 1: assert cursor un-advanced + `emitted==0`. After run 2: assert it emits and the claim reached 2 attempts. |
| **I5** | 🟡 Important | `record_attempt` semantics across `release` are unspecified and untested. `release()` deletes the attempt record (`stores/memory.py:64-67`), so "max_attempts" counts *consecutive in-process* attempts, not lifetime — a fail-release-fail-release message could retry forever and never reach the DLQ. Whether that's intended is undocumented. | `src/mailflow/stores/memory.py:50-67`; `tests/stores/test_memory_stores.py` (`test_dedupe_records_attempts` increments on a live claim only) | Decide the contract (lifetime vs in-process attempts), document it in CLAUDE.md "Contracts" if it crosses modules, then pin it: a test that `record_attempt` after `release` resets, plus a pipeline test that N transient failures across runs eventually DLQ (or intentionally do not). |

### Hardening — behavior is tested per plan; these are extra rigor (optional)

| ID | Sev | Finding | Evidence |
|----|-----|---------|----------|
| **C3** | 🟢 | Size guard isn't *proven* to short-circuit before decoding bytes — `test_oversized…` asserts disposition + cursor only. The code is correctly placed (guard at `pipeline.py:109-115`, before the `try:` that parses), and the plan verifies ordering via code placement + a reviewer note, not a spy. Add a parser/extractor spy that raises if called, to pin §8.6 "DLQ without download." | `tests/core/test_pipeline.py:88-97` |
| **I1** | 🟢 | Malformed / encoded-word / HTML-only / base64-QP / no-filename MIME inputs are untested. Task 7 specifies exactly 5 MIME tests, all on pristine input; the golden corpus has none of these. Branches like the `isinstance(decoded, bytes) else b""` fallback (`mime.py:125`), "first text part wins" (`:131`), and the inline-CID heuristic (`:128`) are never hit. These are the production poison messages. | `tests/extract/test_mime.py`; `src/mailflow/extract/mime.py` |
| **I2** | 🟢 | Envelope edge cases untested: missing `From` fallback (`envelope.py:47`), empty body snippet (`:73`), multiple-From. Task 8 specifies 3 happy-path tests. | `tests/extract/test_envelope.py` |
| **I3** | 🟢 | Bad/absent `Date` header path (`mime.py:69-75` try/except → `date_utc=None`) is never exercised. | `src/mailflow/extract/mime.py:69-75` |
| **S2** | 🟢 | Stdout emitter: assert the `from` alias (not `from_`) survives `by_alias=True` serialization. | `src/mailflow/emit/stdout.py:13`; `tests/emit/test_emitters.py` |
| **S3** | 🟢 | `test_validate_warns_on_unreachable_drop…` asserts `warnings == []` — it tests the *no-warning* case; the warning branch (`loader.py:54-57`) is never triggered. Add the positive case or rename. | `tests/config/test_config.py:52` |
| **S6** | 🟢 | The `RunReport` double-count guard (a frozen contract: `record()` must NOT count `dead_lettered`, only `add_dead_letter()` does) has no direct unit test; only the e2e count of 1 protects it. | `src/mailflow/core/observability.py:46-53` |

### Deferred — plan explicitly pushed to a later plan (NOT gaps)

The plan's deferral list (`docs/superpowers/plans/2026-06-09-mailflow-core-spine.md` lines 13, 3520)
calls these out so the gaps are deliberate, not accidental.

| ID | Finding | Where it lands |
|----|---------|----------------|
| **C2** | Concurrency / same-key dedupe race (two claimants, one emits). The in-memory store is, verbatim, a *"Single-process in-memory model"* that *"does not simulate lease expiry"* — you can't meaningfully test a concurrency invariant against a single-process dict. Phase-1's bar is `test_dedupe_claim_is_exclusive` (sequential), which is correct. | **Plan 2** — Firestore/Redis adapter, where real claim races and lease expiry exist. |
| **S4** | ~~`security.read_allowlist` enforcement is not asserted. The config field round-trips now; enforcement is *"inside provider adapters in Plan 2."*~~ **Withdrawn (field deleted in Phase 1, config-hardening Task 3):** its "fail-closed / empty = read nothing" semantics were inverted from real behavior (default `[]` read everything = fake security), enforcement was deferred to Plan 2, and sender/domain allowlisting is already covered by OnlySender/OnlyDomain/Whitelist filters. Plan 2 reintroduces an enforced version at the point it is applied. | **Plan 2** — provider adapters. |
| — | Attachment streaming, LLM classifier hardening, webhooks/heartbeats/canary/resync, Pub/Sub ordering, plugin allowlist. | **Plans 2 & 4** (see scope note). |

### Withdrawn on review

| ID | Why withdrawn |
|----|---------------|
| **I4** | "Present-but-untrusted Message-ID combined case missing" — incorrect. The plan ships `test_absent_or_malformed_message_id_is_not_trusted` and `test_canonical_id_falls_back_to_stable_hash_when_untrusted` (Task 4), which cover the untrusted → `stable_hash` path. |

### Suite strengths (keep these patterns)

- Three-valued filter logic (accept/reject/abstain + empty-config abstain) and chain short-circuit:
  well covered with value assertions.
- Cursor monotonicity, identity derivation, frozen models, direction/threading: real assertions,
  not truthiness.
- Golden corpus is authored **independently** of the extractor (true cross-check), asserts
  field-level equality, and pins the 16-deep reference chain.
- Each test constructs fresh stores — no inter-test coupling.

### Verdict

The suite is **faithful to the Phase-1 plan and not under-delivering against its own contract.**
Of 15 raw findings: **2 are true Phase-1 gaps (C1, I5)**, the rest are deferred-by-design or
test-rigor upgrades the plan never asked for, and 1 (I4) was withdrawn. Recommended order if we
act: **C1 → I5**, then the hardening sweep (C3, I1–I3, S2/S3/S6) as a Plan-2 warm-up.

---

## Review 2026-06-30 — Attachment safety seam (Task 2)

Reviewer: code review of the just-landed attachment safety seam. Outcome: behaviour is correct as
designed; one correct-but-undocumented footgun captured below. Documentation-only follow-up — no
behaviour change (docstrings + this entry).

| ID | Sev | Finding | Evidence | Scope verdict |
|----|-----|---------|----------|---------------|
| **A1** | 🟡 Low/Medium | The allowlist/scanner safety check governs BOTH real attachments AND inline media (logos, tracking pixels, CID images) — it sits under `is_attachment or is_inline_media`. Because a single blocked part raises and fails the WHOLE message (fail-closed quarantine → DLQ), an allowlist scoped to attachment types (e.g. `{"application/pdf"}`) would also block an inline `image/png` logo and dead-letter otherwise-normal mail. By design (the plan scoped inline-in); only triggers when an operator enables a **non-empty** allowlist (V1 default empty allowlist + no-op scanner = allow-all, so default behaviour is unaffected). Documented in the `safety.py` module docstring + a `mime.py` inline comment. | `src/mailflow/extract/mime.py` safety block (`_walk_body`, the `if is_attachment or is_inline_media:` branch, allowlist/scan check); `src/mailflow/extract/safety.py` (`check_allowlist` + module docstring) | **Documented design caveat** — deferred decision: whether inline media should be exempt from the attachment allowlist or governed by a separate one (P2/follow-up). |

---

## Review 2026-06-30 — Attachment Handling holistic

Reviewer: holistic review of the landed attachment-handling feature. Outcome: one behaviour-neutral
reorder shipped (allowlist-before-decode, commit `97fe433`) + the missing quoted-printable decode
test; the four findings below are notes/caveats logged for follow-up (no behaviour change here).

| ID | Sev | Finding | Evidence | Scope |
|----|-----|---------|----------|-------|
| **A2** | 🟡 | DLQ durable-write amplification: a message under the 50 MB B1 message cap but carrying an over-25MB-per-attachment-cap attachment dead-letters and writes ~40 MB of base64 into the `DeadLetterRecord` (the whole raw message is persisted as `raw_b64`). An attachment just over the per-attachment cap therefore costs a large durable DLQ write. | `core/pipeline.py` `_dead_letter_record` (`raw_b64`); `extract/streaming.py` `MAX_ATTACHMENT_BYTES` | **Deferred** — Plan-2/DLQ hardening (cap or strip the raw bytes persisted on attachment-cap DLQs). |
| **A3** | 🟢 | Fail-closed malformed-base64 DLQ shift: the new streaming path raises `AttachmentUnreadableError` on slightly-malformed base64 that the legacy lenient decode tolerated, dead-lettering the whole message. | `extract/streaming.py` base64 branch | **Documented design caveat** — on-call should expect DLQs for non-conformant senders; not a code bug. |
| **A4** | 🟢 | Allowlist not case-normalized: operator-supplied allowlist entries must be lowercase. `check_allowlist` lowercases the attachment's content-type/extension but NOT the allowlist set, so an entry like `"application/PDF"` silently never matches. | `extract/safety.py` `check_allowlist` | **Hardening** — normalize the allowlist on construction or document the lowercase requirement. |
| **A5** | 🟢 | Redrive cap not threaded: redrive re-runs with `MimeExtractor()`'s default 25 MB cap regardless of the cap that originally DLQ'd the message. | `core/redrive.py` | **Note** — operator-invoked, no auto poison-loop; thread cap config into redrive if/when custom caps are used. |

---

## Review 2026-06-30 — Gmail Subscription Lifecycle holistic

Reviewer: holistic review of the Gmail subscription-lifecycle hardening plan (4 commits). Outcome:
SHIP — the one production change (`should_schedule_renew` extraction) is behaviour-equivalent and the
three tests form an honest coverage story. The notes below are coverage follow-ups (no behaviour
change here). Graph/Outlook subscription lifecycle is a separate deferred plan.

| ID | Sev | Finding | Evidence | Scope |
|----|-----|---------|----------|-------|
| **G1** | 🟢 | No direct `run_service` integration test: the A1 characterization test reconstructs `run_service`'s renew closure (`lambda: renew_watches(...)`) test-locally rather than invoking `run_service`, and A2 tests the extracted guard in isolation. So the live.py guard rewire + the actual lambda's `watch_manager`/`handles` capture have no direct test — a deletion or mis-wire there would keep the suite green. Inherent to "testable without the blocking Pub/Sub consume loop" (which needs the Google SDK). | `tests/test_gmail_reliability.py` (A1 closure), `src/mailflow/adapters/gmail/live.py:257-261` (guard) | **Deferred** — needs a recorded/seam-mocked `run_service` integration harness; out of this unit-hardening pass's scope. |
| **G2** | 🟢 | Asymmetric guard coverage: the renew-scheduling guard was decomposed into the tested `should_schedule_renew` predicate, but the sibling sweep-scheduling guard `if gmail_cfg.sweep_seconds > 0:` was left inline + untested (its runtime behaviour is covered by A3, but the arming decision is not). | `src/mailflow/adapters/gmail/live.py` sweep-guard | **Hardening** — extract a `should_schedule_sweep` predicate for symmetry if the sweep arming logic grows. |
| **G3** | 🟢 | A3's cursor-monotonic assertion is a guard-rail, not an independent test of `commit_if_ahead`'s strict rejection: both push and sweep operate at historyId 200, so it proves "no regression under overlap" but never drives a *lower* order through `commit_if_ahead`. The dedupe (emit-once) assertion carries the real weight. | `tests/test_gmail_e2e.py` overlap test | **Note** — strict-monotonic rejection is covered elsewhere; this assertion is a deliberate guard-rail. |

---

## Review 2026-06-30 — V1 readiness verification (full P0 audit)

Reviewer: V1 verification pass per `docs/architecture-review/v1-verification-prompt.md` — one agent
per checklist item (A×12, B×6, C×2, D×1 = 21), each citing `file:line`. Judged against the **locked
P0 bar** in `features.md` (scope locked 2026-06-29: **Gmail-only live; Outlook/Graph deferred to
fast-follow**), not the verification prompt's pre-lock wording (which still lists A9/C1/C2/A1-Graph).

### Verdict: 🟢 **V1 GO — 0 in-scope blockers.**

Every freeze-now **contract** is frozen correctly; every **Gmail** (the V1 live provider) behavior
path is correct and tested; the **second-provider proof (D1)** is a real working Gmail adapter with a
passing e2e test (`tests/test_gmail_e2e.py`, 21 passed), not a paper audit. **The Gmail part is done
— V1 is considered done for now.** All ✅ items: A1(Gmail) A2 A3 A4 A5 A6 A8 A10 A11 A12 · B1 B2
B3(Gmail) B4(Gmail) B5 · D1.

The findings below are **the complete set of non-✅ marks** — every one resolves to deferred
Outlook/Graph work or an explicitly out-of-V1 item. None gate V1.

### Deferred — Outlook/Graph fast-follow (NOT V1 gaps; Gmail path unaffected)

| ID | Sev | Finding | Evidence | Scope |
|----|-----|---------|----------|-------|
| **VR1** | 🟡 | **B6 — Graph sweep cursor is not page-atomic** (genuine data-loss bug, Graph-only). `delta_sweep` returns one end-of-sequence `deltaLink` and `GraphProvider.sweep` stamps that *same* value on *every* message (only `order` increments). The pipeline commits per terminal disposition, so the end-of-sweep resume token becomes durable after the **first** message is processed — a crash mid-sweep silently drops the fetched-but-unprocessed messages 2..N (Graph resumes past them; dedupe can't save what's never re-delivered). Gmail is correct (per-message historyId cursor is genuinely atomic). | `graph/provider.py:113`, `graph/client.py:75-95`, `core/pipeline.py:148` | **Deferred (Outlook fast-follow)** — flag as a real correctness bug to fix *before* Outlook goes live. Fix: carry the previous resume token on all but the last message of the sweep; emit `new_delta` only on the final one (~5 lines). |
| **VR2** | 🟢 | **A7 — Graph drops `conversationId` thread key.** Contract is frozen (`CleanEmail.thread_key` exists) and Gmail populates it + subject fallback correctly; `GraphExtractor.extract` requests `conversationId` in `$select` but never maps it, so every Graph `thread_key` defaults to `""` with no subject fallback. A7's V1 bar (Contract + Gmail) is met. | `core/models.py:215`, `graph/extractor.py:74-108`, `graph/client.py:16` | **Deferred (Outlook fast-follow)** — cheap fix: map `conversationId` → `thread_key` with `normalize_subject(subject)` fallback in `GraphExtractor.extract` (2 lines) + a Graph-side thread-key test. |
| **VR3** | 🟢 | **B3/B4 — Graph client thinner than Gmail.** Graph `_request` retries **429 only** (not 503/5xx), uses a flat `Retry-After` (no exponential/jitter), has **no concurrency cap**, and a Graph **410** on a per-message GET re-raises as plain `GraphError` → pipeline catch-all bounded-retry rather than straight-to-DLQ Permanent. Gmail fully satisfies B3 and B4. | `graph/client.py:31-53`, `core/pipeline.py:198` | **Deferred (Outlook fast-follow)** — align Graph transport to Gmail: retry `429 or >=500`, exponential+jitter backoff, `BoundedSemaphore`, and map 410→Permanent. |
| **VR4** | 🟢 | **C1 — Graph subscription expiry requests 8640 min** (> the ~4230 ceiling; comment's 10080/7d premise is wrong for message subscriptions). Graph would reject every subscription. Gmail unaffected (daily `watch()`). | `graph/config.py:38` | **Deferred (Outlook live blocker)** — one-liner `subscription_minutes` `8640 → ≤4230`. Already tracked as C1 in `features.md`. |
| **VR5** | 🟢 | **C2 — Graph size-probe absent.** No extended-property (`singleValueExtendedProperty` / PR_MESSAGE_SIZE) probe exists; size is derived from delta JSON and there is no streaming byte-cap. The **B1 fail-closed guard backstops it in V1** (`pipeline.py:170-176` dead-letters unknown/zero size before download, provider-agnostic). | `graph/provider.py:117-124`, `core/pipeline.py:170-176` | **Deferred (Outlook live blocker)** — verify the extended-property probe on a live delta call, or add a streaming download cap. Already tracked as C2 in `features.md`. |

### Out of V1 scope (dropped from P0, not a gap)

| ID | Finding | Where it lands |
|----|---------|----------------|
| **VR6** | **A9 — Store erasure + encryption-context absent.** `BlobStore.put_stream` has no `encryption_context` arg and no `erase()` exists (the only `delete()` is `DeadLetterStore.delete` for redrive cleanup, not GDPR erasure). | **P2** — GDPR DSAR tooling / KMS encryption. Dropped from P0 per Decision #8 (2026-06-29); `core/ports.py:127`. |
| **VR7** | **A1 (Graph) — Graph still fetches by notification payload** (`get_message(user_id, message_id)` from the push), where Gmail re-derives from the durable cursor (wake-signal-only). | **Outlook fast-follow** — the deferred Outlook fetch-rewire; `graph/provider.py:126-131`. Gmail already complies. |

### Note

The verification prompt (`v1-verification-prompt.md`) predates the 2026-06-29 scope lock, so it lists
A9, C1, C2, and the A1-Graph rewire as if they were V1 blockers. They are **not** under the locked
P0 bar — recorded here as deferred so they aren't re-flagged as V1 gaps on the next pass. When the
Outlook adapter is taken live, work VR1–VR5 + VR7 as the fast-follow batch (VR1 first — it's the only
genuine data-loss bug among them).
