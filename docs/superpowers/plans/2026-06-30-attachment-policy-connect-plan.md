# Attachment Policy through `connect()` — Implementation Plan

Executes `docs/superpowers/specs/2026-06-30-attachment-policy-connect-design.md` (rev 2).
Read the spec for rationale; this plan is the task breakdown. Tasks are ordered so the tree
stays importable + mypy-clean + green at every commit.

## Global Constraints (bind every task)

- **Interpreter:** `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest`
  and `… -m mypy` (mypy strict, `packages=["mailflow"]`, whole-tree).
- **TDD, one behavior per cycle:** failing test → confirm it fails for the stated reason →
  minimal impl → green → mypy clean → commit.
- **Importable at every commit:** commit a module before its importers. Never leave the tree
  unimportable. Never `git push`.
- **Commit trailer:** end each message with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Frozen contracts (do NOT break):**
  - Extractor seam `MimeExtractor.extract_bytes(...) -> CleanEmail` signature is unchanged;
    `stripped_attachments` is a field on the returned `CleanEmail`, not a new return value.
  - **A strip is NOT a dead-letter.** Stripping must never call `add_dead_letter()` nor emit a
    `dead_lettered` trace; the DLQ count contract (one poison ⇒ one count, counted only by
    `add_dead_letter()`) is untouched.
  - `attachments=None` / `attachment_policy=None` is byte-for-byte today's behavior, including
    over-cap / blocked / undecodable → DLQ.
  - Schema bumps **1.2 → 1.3** (additive optional field; mirrors the 1.1→1.2 `is_bounce` bump).
- **mypy strict gotchas:** hoist heterogeneous `**dict` splats to a `dict[str, Any]` var;
  narrow `email`/`get_payload` unions with `isinstance` before use; `xs[0] if xs else None`.
- **`StripReason`** values are exactly `not_allowlisted`, `oversize`, `unreadable`, `scanner`.

---

## Task 1 — Core models + schema bump

**Files:** `src/mailflow/core/models.py`, `src/mailflow/core/events.py` (+ tests; + any existing
test/docstring asserting `schema_version == "1.3"`).

Add to `core/models.py`:
- `class StripReason(str, Enum)` with members `not_allowlisted`, `oversize`, `unreadable`,
  `scanner` (value == name).
- `class StrippedAttachment(BaseModel)`: `filename: str = ""`, `content_type: str = ""`,
  `size_bytes: int = 0`, `is_inline: bool = False`, `reason: StripReason` (required).
- On `CleanEmail`: add `stripped_attachments: list[StrippedAttachment] = Field(default_factory=list)`.

In `core/events.py`: bump `SCHEMA_VERSION = "1.3"`; update the docstring example(s) that show
`1.2`. Grep the tree for `"1.2"` schema assertions and update them to `"1.3"` (e.g. wire-event
tests). Do not change `is_bounce`/`is_auto_submitted`.

**Tests:** `StrippedAttachment` requires `reason` and defaults the rest; `CleanEmail()` has
`stripped_attachments == []`; `SCHEMA_VERSION == "1.3"` and an emitted `EmailEvent` reports it.

**Notes:** keep `StripReason`/`StrippedAttachment` in `core/models.py` (no dependency on
`core/ports`, avoids an import cycle). Model = standard model. Cheap-tier transcription once
tests are written.

---

## Task 2 — Attachment policy models + normalization (extract)

**Files:** new `src/mailflow/extract/policy.py` (+ tests).

- `class AttachmentRule(BaseModel)`: `max_bytes: int = MAX_ATTACHMENT_BYTES` (import from
  `extract.streaming`; validate `> 0`), `allowlist: frozenset[str] = frozenset()`,
  `scanner: AttachmentScanner | None = None`. Set `model_config = ConfigDict(arbitrary_types_allowed=True)`
  so the scanner port is allowed. Coerce a list/iterable `allowlist` to `frozenset`.
- `class AttachmentPolicy(BaseModel)`: `real: AttachmentRule = Field(default_factory=AttachmentRule)`,
  `inline: AttachmentRule = Field(default_factory=AttachmentRule)`.
- `def normalize_attachment_policy(value: AttachmentPolicy | AttachmentRule | dict[str, Any] | None)
  -> AttachmentPolicy | None`:
  - `None` → `None`; `AttachmentPolicy` → itself; `AttachmentRule` → `AttachmentPolicy(real=v, inline=v)`;
  - `dict` containing key `"real"` or `"inline"` → `AttachmentPolicy.model_validate(v)`;
  - any other `dict` (bare rule) → `r = AttachmentRule.model_validate(v); AttachmentPolicy(real=r, inline=r)`.

Find `AttachmentScanner`'s import location (it's the port `MimeExtractor` already uses — likely
`mailflow.core.ports`); import from there.

**Tests:** `max_bytes <= 0` raises `ValidationError`; `allowlist=["a","B"]` → `frozenset({"a","B"})`;
`model_validate` round-trips a dict; `normalize_attachment_policy` for each branch
(bare rule/dict → same rule on both classes; `{"real":…,"inline":…}` → per-class).

**Notes:** standard model. Pure pydantic + a small function.

---

## Task 3 — MimeExtractor policy mode (strip dispatch + classification)

**Files:** `src/mailflow/extract/mime.py` (+ tests). Touches the `_walk_body` part loop and
`extract_bytes`/`extract` to carry the stripped list onto the returned `CleanEmail`.

- `__init__`: add `attachment_policy: AttachmentPolicy | None = None`. Raise `ValueError` if
  `attachment_policy` is not None AND any legacy param is non-default
  (`scanner is not None or allowlist != DEFAULT_ALLOWLIST or max_attachment_bytes != MAX_ATTACHMENT_BYTES`).
- **Policy class selector (spec §4.1):** `inline_for_policy = is_inline_media and disp != "attachment"`;
  `rule = policy.inline if inline_for_policy else policy.real`. (Do NOT use the `Attachment.is_inline`
  field for selection — a named CID logo would misroute to `real`.)
- **Dispatch (spec §4.2):**
  - `attachment_policy is None` → exactly today's path (single rule; violations raise → DLQ).
    Unchanged.
  - policy set → per part apply allowlist → cap(`rule.max_bytes`) → `rule.scanner`. On
    `AttachmentBlockedError` from allowlist → strip `not_allowlisted`; `AttachmentTooLargeError`
    → strip `oversize`; `AttachmentUnreadableError` → strip `unreadable`; scanner block → strip
    `scanner`. To strip: do not append to `attachments`, store nothing, append a
    `StrippedAttachment(filename, content_type, is_inline=inline_for_policy, reason, size_bytes=…)`
    (size only when known, e.g. scanner case). Never raise.
- Carry the accumulated stripped list out of `_walk_body` and set
  `CleanEmail.stripped_attachments` in both `extract_bytes` and the port `extract` path.

**Tests:** for a policy, one strip test per reason × {inline, real}: offending part absent from
`attachments`, present in `stripped_attachments` with correct `reason`/`is_inline`, message body +
compliant parts delivered, no blob written for the stripped part. Classification: a named-CID
image (`Content-Type: …; name="logo.png"` + `Content-ID`, no `Content-Disposition`) routes to the
**inline** rule; a `Content-Disposition: attachment` + `Content-ID` part routes to **real**.
Per-class difference: `application/pdf` allowed by `real` but stripped by `inline`. Back-compat:
`attachment_policy=None` still raises (→ DLQ) on over-cap/blocked/undecodable (keep existing tests
green). Mutual exclusion: `MimeExtractor(allowlist={"x"}, attachment_policy=AttachmentPolicy())`
raises `ValueError`.

**Notes:** most capable model — multi-branch control flow over the existing walker. Preserve the
cheap-allowlist-before-decode ordering and the blob-after-checks ordering.

---

## Task 4 — facade `connect()` wiring + public exports

**Files:** `src/mailflow/facade.py`, `src/mailflow/__init__.py` (+ tests).

- `connect()`: add `attachments: AttachmentPolicy | AttachmentRule | dict[str, Any] | None = None`.
  Normalize via `normalize_attachment_policy`. Build the memory pipeline extractor (`facade.py:264`)
  and `_build_gmail_fetcher` extractor (`facade.py:327`) as `MimeExtractor(attachment_policy=pol)`
  when `pol` is not None, else `MimeExtractor()` (unchanged default).
- Export `AttachmentRule`, `AttachmentPolicy`, `StrippedAttachment`, `StripReason` from
  `mailflow/__init__.py` (import `AttachmentRule`/`AttachmentPolicy` from `extract.policy`,
  the two model types from `core.models`).

**Tests:** `connect("memory", seed=…, attachments=AttachmentPolicy(inline=AttachmentRule(max_bytes=…)))`
strips an over-cap inline part end-to-end and delivers the message (assert via the emitted email's
`stripped_attachments`); `connect(..., attachments=AttachmentRule(...))` and
`connect(..., attachments={...})` behave per normalization; the four names import from `mailflow`.

**Notes:** standard model. Integration across facade + exports.

---

## Task 5 — pipeline observability for strips

**Files:** `src/mailflow/core/pipeline.py`, `src/mailflow/core/observability.py` (+ tests).

- Add `attachments_stripped: int = 0` to `RunReport` (and include in its dict dump if it has one).
- In the pipeline, after extraction and before/at emit, read `email.stripped_attachments`; for
  each, increment the counter and emit a decision trace (reason + is_inline) through the existing
  `record(...)` seam — WITHOUT touching DLQ counting (`add_dead_letter` is not involved).

**Tests:** a run that strips N parts reports `attachments_stripped == N` and `dead_lettered == 0`;
a clean run reports `0`. Assert no `dead_lettered` trace is emitted for a strip.

**Notes:** standard model. Respect the DLQ-count contract; a strip is a delivery, not a dead-letter.

---

## Task 6 — gmail live wiring

**Files:** `src/mailflow/adapters/gmail/composition.py`, `src/mailflow/adapters/gmail/live.py`,
`src/mailflow/facade.py` (`_build_gmail_live` passthrough) (+ tests).

Thread an `attachment_policy: AttachmentPolicy | None = None` parameter through
`facade._build_gmail_live` → `run_service` → the composition that builds the extractor
(`composition.py:60`), so the live Gmail pipeline constructs `MimeExtractor(attachment_policy=pol)`.
Default `None` keeps current behavior.

**Tests:** the composition/`run_service` builder constructs the extractor with the supplied policy
(unit-level assertion on the wired extractor); default path unchanged.

**Notes:** standard model. Pure plumbing; keep signatures back-compatible (new kwarg defaults None).

---

## Task 7 — Docs: rewrite "Configuring attachments" in the explorer

**Files:** `examples/feature-explorer-v2.html`.

Replace the "Configuring attachments" section's "not exposed yet / build a Pipeline directly"
caveat with the first-class `connect(attachments=…)` API: the bare simple case
(`attachments={"max_bytes":…, "allowlist":[…]}` applies to both classes) and the per-class
`AttachmentPolicy(real=…, inline=…)` form. Add the classification caveat (named-CID media →
inline rule) and the strip-and-deliver semantics with the loud opt-in note (a configured message
strips + records instead of DLQ; default stays DLQ). Update the "attachment cap" always-on chip
tooltip accordingly, and the footer schema note to 1.3. Keep the fail-behavior warning, reframed
for strip-vs-DLQ. Snippets must match the shipped API (verify against `facade.connect` signature
and `extract/policy.py`).

**Tests:** none (static HTML); after editing, extract the `<script>` and run `node --check`, and
grep that no snippet references a non-existent kwarg.
