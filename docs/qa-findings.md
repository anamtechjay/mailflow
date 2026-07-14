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

## Review 2026-06-30 — Extraction layer edge-case & failure-case pass

Reviewer: added `tests/test_extract_edge_cases.py` — 54 edge/failure tests over the extractor,
identity surrogate, attachment handling, and classification seam (the parts that take untrusted,
malformed wire input). Outcome: **52 pass, 2 documented `xfail`s** surfacing the two defects below.
Full suite 405 passed / 2 xfailed; mypy --strict clean. The 52 passing tests confirm the extractor is
robust to: missing Message-ID/From/Subject/Date, malformed Date, empty/headers-only body, duplicate
From, base64/quoted-printable/RFC2047 bodies, nested multipart, inline-vs-real attachments,
no-filename attachments, content-addressed dedup, policy strip-on-oversize, and the bounce/
auto-submitted heuristics.

| ID | Sev | Finding | Evidence | Scope |
|----|-----|---------|----------|-------|
| **E-1** | 🟡 | Unknown-charset body crashes extraction: a `text/plain` part declaring an unregistered charset (`charset=x-totally-made-up`) raises `LookupError` from `EmailMessage.get_content()` — the extractor does not fall back to `errors='replace'`. A malformed-charset email therefore poisons to the DLQ instead of being delivered with a best-effort body. Real spam / misconfigured senders hit this. | `extract/mime.py` `_walk_body` → `part.get_content()` (~line 199/202); `tests/test_extract_edge_cases.py::test_unknown_charset_does_not_crash` (xfail) | **Phase gap** — wrap `get_content()` to catch `LookupError` and decode bytes with `errors='replace'`. Cheap, high-value robustness fix. |
| **E-2** | 🟢 | Inconsistent `is_inline` across paths: the kept `Attachment` uses `is_inline = is_inline_media and not is_attachment`; policy mode uses `inline_for_policy = is_inline_media and disp != "attachment"`. A part with a `Content-ID` AND a `name=` but no `Content-Disposition` is `is_inline=False` when delivered yet `is_inline=True` when stripped — same part, two answers. | `extract/mime.py` `_walk_body` (meta is_inline ~line 210 vs `inline_for_policy` ~line 217) | **Hardening** — derive `is_inline` once and share it between the kept-Attachment and StrippedAttachment paths. Low severity (real inline parts carry `Content-Disposition: inline`, on which both paths agree). |

---

## Review 2026-07-06 — Logging destinations & observability sinks

New feature (local): `enable_logging()` + NullHandler default; `connect(log_file=, log_level=,
on_report=, on_trace=)`; `Observers` threaded into Pipeline (fires from `_record` per trace and
`run_once` per report) and all three live providers. Callbacks are guarded (throwing callback
logged + swallowed, ingestion unaffected). Defaults off → backwards compatible.
**CONTRACT DECISION:** deliberate core edit (`observability.py`, `pipeline.py`) — non-emitted
dispositions are only visible in the pipeline, so an emitter wrapper cannot capture the audit.

---

## Review 2026-07-07 — QA automation suite

New test-only build (local, uncommitted): ~76 pytest tests across `tests/qa/test_f01_*`
through `test_f17_*`, four `test_scenarios_*` files, and this phase's `test_property_invariants.py`,
driven by a shared harness (`tests/_harness/fakes.py::build_memory_pipeline`,
`tests/_harness/email_builder.py`, `tests/_harness/corpus.py`) per the coverage map in
`docs/qa-partA-coverage.md`. Full fast suite: **637 passed, 4 deselected (slow), 2 xfailed**
(the two pre-existing `E-1`-adjacent xfails from the 2026-06-30 extraction-edge-case pass).
`mypy --strict` clean (86 source files; `packages = ["mailflow"]` scopes to `src/`, tests are
not mypy-checked, consistent with prior phases). `hypothesis` installed cleanly (6.156.1), added
to `dev` extras.

**CI tiers** (as designed across Phases 0-5, none of this is wired into an actual CI config file —
that's a follow-up, not part of this pass):
- **commit** (every push): `pytest -m "not slow and not live"` + `mypy` — the 637-test fast suite
  above, sub-6s, no external dependencies.
- **nightly**: `pytest -m slow` — bulk-volume (`test_bulk_10k_emitted_once`), subprocess-restart,
  and other large/slow scenario tests.
- **gated** (manual/scheduled, requires real credentials): `pytest -m live` — no test currently
  carries this marker; it's registered in `pyproject.toml` ahead of the first live-provider test
  (Plan 2/3 territory) so that test doesn't also need a `pyproject.toml` edit.

### Findings surfaced while building the property/fuzz tests (not patched — logged only)

| ID | Sev | Finding | Evidence | Scope verdict |
|----|-----|---------|----------|---------------|
| **P1** | 🟡 Important | Retry-exhaustion is unreachable via `InMemoryDedupeStore`: `release()` deletes the attempt record entirely, so the next `try_claim` on the same key resets `attempts` to 0 on the following `record_attempt()` call — attempts never accumulate past 1 across separate `run_once()` calls (the real-world redelivery path), so dead-lettering-by-attempt-exhaustion at the default `max_attempts` can never be driven through the public store contract. Corroborates existing finding **I5** (2026-06-09 review) via an independent code path (found while designing the exactly-once property test, which needed to reason about repeated claims of the same key). | `src/mailflow/stores/memory.py` (`release`/`record_attempt`); `src/mailflow/core/pipeline.py` `_retry_or_dead_letter` (the `attempts >= max_attempts` branch) | **Deferred/duplicate of I5** — same root cause, not a new gap; do not action separately from I5. |
| **P2** | ⚪ Low/doc | `Envelope.date_utc` is never populated by `MimeEnvelopeParser.parse_envelope` (only `MimeExtractor.extract_bytes` parses `Date`, per the note already in `tests/qa/test_f03_parse.py`) — any envelope-level (pre-extract) assertion about `date_utc` is trivially `None`. Documentation-only; no test gap since Phase 1/QA already places date-parsing assertions against the extractor, not the parser. | `src/mailflow/extract/envelope.py` (`parse_envelope` builds `Envelope(...)` with no `date_utc` field set); contrast `src/mailflow/extract/mime.py` `Date` try/except | **Doc note** — no action; matches the already-adopted test placement. |
| **P3** | ⚪ Low | `MimeEnvelopeParser.parse_envelope`'s `_snippet()` calls `body.get_content()` on the preferred text/html part with no charset fallback: a `text/plain` part declaring an unregistered charset (e.g. `charset=x-totally-made-up`) raises `LookupError` out of `parse_envelope` itself, before extraction even runs. This is the same defect class as **E-1** (2026-06-30 review, which documented it in `extract/mime.py`) but on the pre-filter parse path — confirmed empirically (hand-crafted input) while scoping the "known exception" allowlist for `test_parser_never_crashes_on_random_bytes`; not found by the random-bytes fuzz itself (~7500 examples), since hitting it needs syntactically valid header structure that pure `st.binary()` essentially never produces. | `src/mailflow/extract/envelope.py` `_snippet` (`body.get_content()`); test allowlists `LookupError` accordingly rather than failing the fuzz run | **Phase gap (narrow), extends E-1** — same fix (`errors="replace"` fallback) would resolve both `mime.py` and `envelope.py` in one pass; low real-world frequency (malformed charset declarations), not patched here per scope (tests/docs only). |

## Resolution 2026-07-07 — REL-3: I5 + P1 fixed (silent unbounded retry, poison never DLQs)

**I5** and **P1** are now **FIXED**, not deferred. Confirmed red first: a poison message whose
extraction always raises `TransientError`, driven across 5 separate `Pipeline`/`run_once()` calls
(the real redelivery path) over shared `InMemoryDedupeStore`/`InMemoryCursorStore` at the default
`max_attempts=3`, never dead-lettered — `attempts` reset to 0 on every redelivery because
`release()` deleted the whole claim record. Reproducer: `tests/qa/test_rel3_retry_exhaustion.py`.

**Fix**: `InMemoryDedupeStore`/`SqliteDedupeStore` `release()` now clears only the `claimed` flag
(memory: `_ClaimRecord.claimed = False`; sqlite: `UPDATE claims SET claimed = 0 WHERE key=? AND
done=0`) and keeps the record — `attempts` is no longer wiped. `try_claim` now succeeds when the
key is absent OR present-but-not-claimed-and-not-done. **CONTRACT DECISION**: `attempts` is a
LIFETIME counter per key (accumulates across claim/release cycles, i.e. across redeliveries), not
a per-in-process-claim counter. External store method shapes (`try_claim`/`record_attempt`/
`mark_done`/`release`) are unchanged, so a real Firestore/Redis adapter can still add lease expiry
without touching callers.

**Test encoding the OLD (buggy) contract, updated**: `tests/qa/test_f01_dedupe.py
::test_release_resets_attempt_count` asserted attempts reset to 0 after release+re-claim; renamed
to `test_release_keeps_lifetime_attempt_count` and now asserts attempts carry over (3rd
`record_attempt()` after a release+re-claim returns 3, not 1). No other test asserted the old
delete-on-release shape (`tests/test_sqlite_stores.py::test_dedupe_release_frees_undone_claim` /
`test_dedupe_release_keeps_done_claim` only assert claim-freed/claim-still-blocked behavior, which
is unchanged and still passes).

---

## Review 2026-07-13 — Live Graph mailbox smoke check (manual, not a unit-test pass)

Reviewer: ad-hoc live verification against the real `techjaystest4@nsrecycle.com` Exchange
Online mailbox (app-only Graph creds), run outside pytest to sanity-check the Graph adapter
against actual API behavior the fake transport can't model. Not a coverage review of existing
tests — a live-data check that happened to surface a real bug.

### Actionable now — genuine bug, fixed

| ID | Sev | Finding | Evidence | Fix |
|----|-----|---------|----------|-----|
| **G1** | 🔴 Critical | `GraphClient.list_attachments` requested `$select=id,name,contentType,size,isInline,contentId` on `/messages/{id}/attachments`. `contentId` is a property of the `fileAttachment` subtype only, not the base `microsoft.graph.attachment` type this list endpoint returns — Graph responded `400: Could not find a property named 'contentId' on type 'microsoft.graph.attachment'` on **every** message with an attachment. This is called from `GraphProvider._attach`, inside `provider.fetch()`, i.e. **before** `Pipeline._process`'s own try/except — so in the live notification-fed path this would crash `run_once()` outright (not merely dead-letter one message) the first time any attachment-bearing mail arrived. `_FakeGraphTransport` never modeled this Graph `$select`-on-polymorphic-type validation rule, so no existing unit test could catch it — confirmed by live reproduction, not by test failure. | `src/mailflow/adapters/graph/client.py:68-71` (pre-fix); reproduced live via direct Graph REST call, HTTP 400 | Fixed: dropped `contentId` from the `$select` (`GraphExtractor._attachments`, `src/mailflow/adapters/graph/extractor.py:54`, already does `a.get("contentId") or ""` — tolerates its absence). Re-ran `tests/qa/test_f17_fetch.py` (3/3 pass, no regression) plus a live `Pipeline.run_once()` against 10 real messages (4-attachment cases included): `RunReport: fetched=10 emitted=10 dropped=0 duplicates=0 dead_lettered=0`. |

### Scope verdict

**Phase gap in the live/Azure-API sense, not the unit-test sense** — the unit suite is deliberately
100% offline (§1 of `testing-guide.md`), so this was never going to be caught there; it's a gap in
*fixture fidelity* (the fake transport doesn't model Graph's per-type `$select` validation), not in
test-count coverage. No new unit test added for the fake-transport gap itself (would require
teaching `_FakeGraphTransport` Graph's real OData type-validation rules — logged here as a
follow-up idea, not actioned, since the live check already proves the real fix). Full narrative in
`docs/mailflow-final-report.md` §4 and `docs/testing-guide.md` §8.1.
