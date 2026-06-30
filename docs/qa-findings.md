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
| **S4** | `security.read_allowlist` enforcement is not asserted. The config field round-trips now; enforcement is *"inside provider adapters in Plan 2."* | **Plan 2** — provider adapters. |
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
