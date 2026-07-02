# Consumer-owned filter policy: `on_filtered` (tag vs drop)

**Date:** 2026-07-02
**Status:** Design — awaiting review
**Scope:** one connect-level knob that moves the drop-vs-deliver decision from the library to the consumer.

## Problem

Today the pipeline treats a filter match as a terminal **drop**: the matched message is
never fully extracted, never delivered, and its useful signals (`is_bounce`,
`is_auto_submitted`, which filter matched, why) are computed and then discarded. The
disposition survives only in the in-memory `RunReport.traces` plus a log line.

Two problems fall out of this in a real deployment:

1. **Silent loss.** "A user swears they emailed us; it's nowhere; we can't explain why."
   The library made a policy decision (drop bounces / list mail) silently, and left no
   durable, consumer-visible record.
2. **Misplaced policy.** *Whether a bounce matters* is a consumer decision — a CRM cares
   about bounces, a newsletter reader does not — but the library decides for everyone.

The fix: stop silently dropping. Deliver everything by default, **tagged** with why it was
flagged, and let the consumer decide keep/skip/route. Keep drop available as an opt-in for
consumers who genuinely want the old behavior.

## Goals

- A single connect-level setting: `on_filtered: "tag" | "drop"`, defaulting to `"tag"`.
- In `tag` mode, a filter-matched message is delivered like any other email, carrying a
  disposition tag + reason, so the consumer can branch on it.
- In `drop` mode, behavior is exactly as today.
- No change to correctness bookkeeping (cursor monotonicity, dedupe, one-emit-per-message).

## Non-goals (explicit — these are follow-up specs)

- **Durable dispositions / DLQ in sqlite.** Persisting dropped/duplicate/dead-lettered
  records and wiring `SqliteDeadLetterStore` is a separate spec.
- **Live disposition hooks** (`on_dropped` / `on_dead_letter`). Out of scope here; the
  unified stream already covers the tag use-case.
- **Durable token rotation.** Separate spec.
- **Duplicate and dead-letter handling.** `on_filtered` governs **filter matches only**.
  Duplicates are still suppressed (delivering the same email twice, even tagged, breaks the
  idempotency contract and is rarely wanted). Dead-letters (processing *failures*) are
  unchanged — a failure is not a policy decision.

## API surface

One new keyword argument on `connect()` (`src/mailflow/facade.py`):

```python
from typing import Literal

def connect(
    provider: str,
    *,
    ...,
    on_filtered: Literal["tag", "drop"] = "tag",
) -> Mailflow: ...
```

- `"tag"` (default): filter-matched messages are extracted, cleaned, run through
  `stages`/`clean_fn`, and delivered through the normal emitter, tagged.
- `"drop"`: filter-matched messages are dropped (recorded, cursor advances, not delivered)
  — today's behavior.

Consumer code:

```python
mf = connect("gmail", credentials=creds, mailbox="me")   # tag mode (default)
for email in mf.stream():
    if email.disposition == "filtered":
        archive_but_dont_process(email)     # consumer decides: keep / skip / route
    else:
        handle(email)
```

The setting flows to `PipelineConfig` (see below); it applies to both the memory and Gmail
provider paths since both build the same `Pipeline`.

## Model changes

Two fields added to `CleanEmail` (`src/mailflow/core/models.py`). `matched_filter` already
exists and is reused.

```python
disposition: Literal["emitted", "filtered"] = "emitted"
filter_reason: str = ""      # e.g. "list/auto-submitted header present"
```

Rationale for the narrow `disposition` type: a `CleanEmail` in the consumer's hands was, by
definition, delivered — so it can only be `emitted` (passed filters) or `filtered` (matched a
filter but delivered in tag mode). The internal `Disposition` enum's other states
(`duplicate`, `dead_lettered`) describe messages the consumer never receives, so they are
deliberately **not** representable here. This prevents impossible states on the delivered
object.

Both new fields are ordinary `CleanEmail` fields, so they are automatically:
- projectable via `fields=[...]` (`make_projection` reads `CleanEmail.model_fields`), and
- available to `stages` / `clean_fn` post-processing.

## Pipeline behavior

`PipelineConfig` gains a field carrying the mode:

```python
on_filtered: Literal["tag", "drop"] = "tag"
```

In `Pipeline._do_work` (`src/mailflow/core/pipeline.py`), the `Decision.drop` branch forks:

- **`on_filtered == "drop"`** — unchanged: `mark_done`, record `Disposition.dropped` with
  `matched_filter` + `reason`, return `Disposition.dropped`.
- **`on_filtered == "tag"`** — do **not** early-return. Fall through to the normal
  extract → tag → emit path:
  1. `email = self._extract(msg, env)`
  2. stamp `email.disposition = "filtered"`, `email.matched_filter = decision.filter_name`,
     `email.filter_reason = decision.reason`
  3. run the same attachment-strip recording, event construction, `emitter.emit`,
     `dedupe_store.mark_done`, and trace recording as a normal emit.

Emitted (untagged) mail continues to set `disposition = "emitted"` (the default) — no code
change needed beyond the default.

### Counting (contract decision — resolved: option A)

CLAUDE.md freezes the `RunReport` disposition counters. A tagged-and-delivered message *was
delivered*, so it is counted as **`emitted`** in `RunReport`, and the fact that it was
filter-tagged is preserved in its `DecisionTrace` (`matched_filter`, `reason`) — the same
trace fields already used for drops.

- **Chosen (A):** reuse `emitted` for the count; distinguish via the trace and the new
  `CleanEmail.disposition` field.
- **Rejected (B):** add a distinct `Disposition.filtered` counter. Cleaner accounting but
  changes the frozen enum and every `RunReport` consumer — larger blast radius for no
  behavioral gain, since the per-email tag already carries the truth.

This honors the existing "delivered ⇒ exactly one `emitted`" invariant.

### Trace stage

The tag-path `DecisionTrace` uses `disposition = Disposition.emitted` but stage/reason make
the filter match visible. Proposed: `stage = "emit"` with `matched_filter`/`reason` set (so
`RunReport.emitted` and the trace agree). The audit trail therefore shows *which* emitted
messages were filter-tagged and why.

## Interaction with existing seams

- **Cursor:** advances on the emit (a terminal disposition), exactly as a normal emit —
  monotonic `commit_if_ahead` unchanged.
- **Dedupe:** `mark_done` is called on the emit path — a tagged message is not reprocessed.
- **Attachment strip:** tagged emails go through the same `stripped_attachments` recording as
  emitted ones.
- **`stages` / `clean_fn` / `cleaner`:** applied to tagged emails identically to emitted
  ones (they flow through the same `pipe_emitter`).
- **`fields=` projection:** `disposition` and `filter_reason` are selectable like any field.

## Backward compatibility

**This flips the effective default.** Today, a configured filter drops matches; after this
change, the default (`on_filtered="tag"`) *delivers* them tagged. Consumers who rely on
filters removing mail from the stream must now either:

1. branch on `email.disposition == "filtered"` and skip those themselves, or
2. pass `on_filtered="drop"` to restore prior behavior.

This is an intentional, safer default (never silently lose mail). It is called out here so it
is a conscious migration, not a surprise. The project is pre-1.0 / POC, so a default change is
acceptable; it will be noted in the changelog and README.

## Ownership / cross-module coordination

Per CLAUDE.md ownership, this spec touches files owned by two teammates plus the facade:

- `core/models.py` (core-foundation-engineer) — new `CleanEmail` fields.
- `core/pipeline.py` + `PipelineConfig` (pipeline-engineer) — the tag fork + config field.
- `facade.py` — new `connect(on_filtered=...)` param, threaded into `PipelineConfig`.

Because `pipeline.py` imports the `CleanEmail` fields, land the `core/models.py` change first
(keep the tree importable per the strict-mypy whole-tree rule).

## Testing plan (TDD, one behavior per cycle)

1. `CleanEmail` defaults: `disposition == "emitted"`, `filter_reason == ""` on a normal
   extract.
2. Memory pipeline, `on_filtered="drop"`: a filter-matched message is **not** delivered;
   `RunReport.dropped == 1` (regression guard — old behavior intact).
3. Memory pipeline, `on_filtered="tag"` (default): the same matched message **is** delivered
   with `disposition == "filtered"`, `matched_filter` set, `filter_reason` set;
   `RunReport.emitted` counts it (option A); `RunReport.dropped == 0`.
4. Cursor/dedupe: in tag mode the tagged message advances the cursor and is `mark_done` (not
   redelivered on a second pass).
5. `fields=["disposition", "filter_reason"]` projects the tag correctly.
6. `on_email` callback receives the tagged email in tag mode.
7. mypy --strict clean on the `Literal` additions.

## Open questions

- Naming: `on_filtered` vs `filter_mode` vs `on_match`. Current pick: `on_filtered`
  (reads well: "on filtered, tag"). Confirm before implementation.
