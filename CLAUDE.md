# mailflow — teammate conventions (every agent-team teammate reads this)

Captured from the Phase-1 build so you don't rediscover it. The plan is the source of truth
for *what* to build; this file is *how we work* + the gotchas + the contracts already frozen.

## Environment
- Project root: `/Users/iamanam/projects/techjays/poc/mailflow` (git, src-layout).
- **Always** use the venv interpreter for tests/types:
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest`
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
- Installed: pydantic 2.13, pytest 9, mypy (strict). **PyYAML is NOT installed** — the config
  loader falls back to JSON; keep any YAML fixtures JSON-compatible (JSON ⊂ YAML).

## Workflow — TDD, non-negotiable
Failing test → confirm it fails for the stated reason → minimal impl → green → `mypy` clean →
commit. One behavior per cycle. The plan's code snippets are **illustrative**: they may contain
placeholder artifacts (an `_unused` field, duplicated inlined lines) and may **not** pass mypy
strict verbatim. Apply the minimal behavior-preserving fix and record it in your report.

## mypy --strict gotcha catalog (apply proactively — all hit in Phase 1)
- **`**{"from": x}` splat** into a model with heterogeneous kwargs infers `dict[str, X]` and is
  rejected → hoist to an explicitly `dict[str, Any]`-typed var before splatting.
- **`email…get_payload(decode=True)` / `get_content()`** return unions/`Any` → narrow with
  `isinstance(payload, bytes)` before `hashlib`; wrap text in `str(...)` to kill `no-any-return`.
- **`(xs or [None])[0]`** trips strict typing → write `xs[0] if xs else None`.
- **Richer-than-port signature** (e.g. `MimeExtractor.extract_bytes` vs the `ContentExtractor.
  extract(msg, env)` port) won't type-check where the port type is annotated → widen the
  annotation to a union (`ContentExtractor | MimeExtractor`) and `isinstance`-dispatch.
- mypy is configured `packages = ["mailflow"]`, strict — it checks the whole importable tree, so
  **every commit must leave the tree importable**. If module A imports B, commit B first
  (reorder within your scope even if the plan lists them the other way — bisectability matters).

## Shared-repo discipline (we run as subagents in ONE git repo)
Real agent-teams isolate teammates in git worktrees; in subagent mode we share one repo, one
`.git/index`, one `.mypy_cache`. So **the lead serializes file-disjoint tracks** — do not assume
another engineer is committing concurrently; while your task runs, treat the repo as yours.
Commit messages: the plan's message + trailing
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. **Never `git push`.**

## Ownership — never edit another teammate's files
| Teammate | Owns |
|---|---|
| core-foundation-engineer | `core/{errors,models,identity,events,ports,filtering,observability}.py` |
| extract-filter-engineer | `extract/`, `filters/` |
| adapters-engineer | `stores/`, `providers/`, `emit/` |
| pipeline-engineer | `core/pipeline.py`, `config/`, `registry.py`, `builder.py`, `__init__.py` |
| fixtures-golden-engineer | `tests/fixtures/`, golden + e2e tests, top-level `README.md` |
| plan-conformance-verifier | read-only auditor |

If you need a change in someone else's file, **message them + the lead** — do not patch it.

## Surfacing contract decisions (this is mandatory, not optional)
If you resolve an ambiguity or a contradiction in the plan that affects other modules, do NOT
decide silently. Flag it in your final report under **CONTRACT DECISION** with the downstream
impact so the lead can propagate it. (Phase-1 example below.)

## Contracts already frozen — honor them, don't relitigate
- **Extractor seam:** `MimeExtractor.extract_bytes(raw, *, provider, provider_message_id,
  stream_id, watched_mailbox) -> CleanEmail`. `Pipeline._extract` dispatches: a `MimeExtractor`
  → `extract_bytes`; any other extractor → the port `extract(msg, env)`. **Plan 2's Graph
  extractor implements `extract(msg, env)` directly** — that's why the seam exists.
- **DLQ counting:** `RunReport.dead_lettered` is incremented **only** by `add_dead_letter()`;
  `record()` does NOT count it. `Pipeline._dead_letter` must call `add_dead_letter()` **and**
  `record(_trace(...dead_lettered...))` as a paired unit; never emit a standalone `dead_lettered`
  trace elsewhere (it would be miscounted). One poison/oversized message ⇒ exactly one count.
- **Cursor:** `commit_if_ahead` is strictly monotonic (rejects order ≤ stored). Cursor advances
  on **every** terminal disposition (emitted/dropped/duplicate/dead_lettered), not only emit.
- **Dedupe:** exactly `try_claim / record_attempt / mark_done / release`. The in-memory store
  does **not** simulate lease expiry — keep the shape exact so the real Firestore/Redis adapter
  (Plan 2) can add expiry without changing callers.
- **Identity:** `idempotency_key = (tenant, mailbox, provider_message_id)`; `canonical_id` =
  trusted `<...@...>` Message-ID else `stable_hash(provider, provider_message_id, mailbox)`.
- **Type-consistency anchor:** `parse_envelope(msg, tenant)`, `message_size(msg)`,
  `EmailEvent(schema_version, tenant, ordering_key, email)`, `SCHEMA_VERSION == "1.0"`, the
  `from_`↔`from` alias on `Envelope`/`CleanEmail`. Spelled identically everywhere.

## Fixtures / golden discipline
The golden corpus is authored **independently** of the extractor (cross-check). RFC822
round-trip artifacts in the fixture **loader** (e.g. long Message-IDs get header-folded and
re-parse with a leading space) are **loader bugs** — fix the loader (no-fold policy,
`max_line_length=998`), never weaken the golden or "fix" a correct extractor.

## QA findings — log them, don't lose them in chat
All QA / test-coverage review findings live in **`docs/qa-findings.md`**. When you run a QA pass
(`/pr-review-toolkit:review-pr`, a test audit, etc.):
- **Read `docs/qa-findings.md` first** — don't re-report something already logged, withdrawn, or
  marked deferred.
- **Append** new findings there (don't leave them only in the transcript). Give each an ID,
  severity, evidence (`file:line`), and a **scope verdict**.
- **Classify every coverage gap against Phase scope** (`docs/superpowers/plans/…`), not the spec's
  full §8 ambition. Buckets: **Phase gap** (shipped code, no test — actionable now) /
  **Deferred** (plan pushed it to a later plan — not a gap) / **Hardening** (tested per plan, extra
  rigor) / **Withdrawn** (already covered). The spec spans 4 plans; each phase ships a narrowed
  slice — judging against the wrong bar manufactures false gaps (see the 2026-06-09 review).

## Required report format (your final message IS the result)
Return, tersely:
1. Tasks completed + commit hashes (`git log --oneline`).
2. Full `pytest` counts + `mypy` result **verbatim**.
3. Deviations from the plan, each with a one-line reason.
4. **CONTRACT DECISION(s)** — any cross-module ambiguity you resolved + downstream impact.
5. Exported names / entry-point signatures downstream teammates depend on.
