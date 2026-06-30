# Per-class Attachment Policy through `connect()` — Design

**Date:** 2026-06-30
**Status:** Approved (design); implementation plan to follow.
**Scope:** Expose attachment size/type/scanner policy through the programmatic `connect()`
API, with **separate configuration for inline media vs. real attachments**, and a
**strip-and-deliver** disposition (opt-in) instead of dead-lettering.

---

## 1. Problem

The attachment safety knobs already exist on `MimeExtractor.__init__`
(`scanner`, `allowlist`, `max_attachment_bytes`), but every `connect()` path and
`build_from_config` hardcode a bare `MimeExtractor()` with defaults, so a user **cannot
configure attachments through the public API** today. Two further gaps:

- There is **one** policy for all non-body parts. Inline media (logos, tracking pixels,
  CID images) and real attachments (`Content-Disposition: attachment`) cannot be governed
  separately, even though the MIME walker already distinguishes them
  (`Attachment.is_inline`).
- The only failure mode is **fail-closed → DLQ**: a single over-cap or disallowed part
  dead-letters the *entire* message — undesirable for, e.g., an oversized inline pixel.

## 2. Decisions (locked during brainstorming)

| # | Decision |
|---|----------|
| D1 | **Disposition when configured:** strip the offending part (inline *or* real) and deliver the message; never dead-letter for attachment policy. |
| D2 | **Opt-in:** with no attachment config, behavior is **unchanged** — over-cap/blocked parts still raise → DLQ (existing tests + the "attachment cap" doc chip stay true). Strip-and-deliver activates only when policy is supplied. |
| D3 | **Scope:** programmatic `connect()` only. `build_from_config` (YAML/JSON) is a deliberate follow-up; the scanner is a live Python object and cannot live in a config file. |
| D4 | **API shape:** typed pydantic models `AttachmentPolicy` + `AttachmentRule`; `connect()` also accepts an equivalent plain `dict` (coerced via pydantic). |
| D5 | **Recording:** a stripped part is **not silent** — it is recorded via observability (a `RunReport` counter + a decision trace, reason ∈ {`oversize`, `not_allowlisted`, `scanner`}). **No wire-schema change** (`EmailEvent.schema_version` stays `"1.2"`; the stripped part is simply absent from `email.attachments`). A consumer-facing `stripped_attachments` field on `CleanEmail` is explicitly **deferred** (would bump the schema to `1.3`). |

## 3. Public API

New exports from `mailflow` (`__init__.py`):

```python
from mailflow import AttachmentPolicy, AttachmentRule
```

```python
class AttachmentRule(BaseModel):
    max_bytes: int = 25_000_000                       # > 0 (validated)
    allowlist: frozenset[str] = frozenset()           # content-type OR lowercased ext; ∅ = allow all
    scanner: AttachmentScanner | None = None          # arbitrary_types_allowed; None = no-op (allow all)

class AttachmentPolicy(BaseModel):
    real:   AttachmentRule = AttachmentRule()         # Content-Disposition: attachment
    inline: AttachmentRule = AttachmentRule()         # inline / CID media
```

`connect()` gains one keyword:

```python
def connect(..., attachments: AttachmentPolicy | dict[str, Any] | None = None) -> Mailflow
```

- `None` (default) → today's behavior, fully unchanged.
- `dict` → `AttachmentPolicy.model_validate(attachments)`.
- An omitted class (e.g. only `real=` given) defaults to allow-all / 25 MB for the other,
  still under strip-mode.

Example:

```python
from mailflow import connect, AttachmentPolicy, AttachmentRule

mf = connect("gmail", credentials=creds, mailbox="me",
    attachments=AttachmentPolicy(
        real   = AttachmentRule(max_bytes=10_000_000, allowlist={"application/pdf"}, scanner=AvScanner()),
        inline = AttachmentRule(max_bytes=1_000_000,  allowlist={"image/png", "image/jpeg"}),
    ))
```

## 4. Extractor seam

`MimeExtractor.__init__` gains an optional, **additive** parameter:

```python
def __init__(self, *, scanner=None, allowlist=DEFAULT_ALLOWLIST,
             max_attachment_bytes=MAX_ATTACHMENT_BYTES,
             attachment_policy: AttachmentPolicy | None = None) -> None
```

Per non-body part the walker already computes `is_inline`. Dispatch:

- **`attachment_policy is None`** → existing path: single `scanner`/`allowlist`/
  `max_attachment_bytes`; a violation **raises** (`AttachmentTooLargeError` /
  `AttachmentBlockedError`) → DLQ. **No behavior change.**
- **`attachment_policy` set** → choose `rule = policy.inline if meta.is_inline else policy.real`;
  apply, in the existing cheap-first order:
  1. allowlist check on metadata (before decode);
  2. decode + streaming cap at `rule.max_bytes`;
  3. `rule.scanner.scan(meta)`.
  On **any** violation: **strip** the part (do not append to `attachments`, store nothing),
  record it (D5), and continue. The body and all compliant parts are delivered.

Notes:
- The streaming cap already aborts mid-stream without buffering, so a stripped over-cap part
  costs no extra memory; the over-cap `AttachmentTooLargeError` is caught and converted to a
  strip **only** in policy/strip-mode.
- Blobs are written only *after* a part passes all three checks, so a stripped part never
  leaves a partial blob.

## 5. Wiring

Replace the bare `MimeExtractor()` with a policy-aware one in the three `connect()` paths:

| Path | File:line (today) | Ownership |
|------|-------------------|-----------|
| memory pipeline | `facade.py:264` | pipeline-engineer |
| gmail fetch-by-id | `facade.py:327` (`_build_gmail_fetcher`) | pipeline-engineer |
| gmail live | `adapters/gmail/composition.py:60` (built for `run_service`) | adapters-engineer |

`connect()` normalizes `attachments` → `AttachmentPolicy | None`, threads it into all three.
The gmail live path requires threading the policy through `_build_gmail_live` → `run_service`
→ `composition`. **Cross-ownership:** touches `core/models.py` (+ `__init__` exports),
`extract/`, `facade.py`, `adapters/gmail/`, and `core/observability.py` — the plan will
sequence commits so the tree stays importable at each step (mypy strict, whole-tree).

## 6. Recording stripped parts (observability)

- Add a `RunReport` counter (e.g. `attachments_stripped: int`) and emit a decision trace per
  strip with `reason ∈ {oversize, not_allowlisted, scanner}` and the part class (inline/real).
- This reuses the existing `record(trace)` seam; it does **not** touch the DLQ count contract
  (a strip is not a dead-letter). `core/observability.py` is core-foundation-owned — flagged.
- **No** `EmailEvent`/`CleanEmail` field is added (schema stays `1.2`).

## 7. Testing (TDD)

1. `AttachmentRule` validation: `max_bytes <= 0` rejected; `allowlist` accepts list→frozenset;
   `AttachmentPolicy.model_validate(dict)` round-trips.
2. Strip behavior, policy set, one test per violation type (oversize / not-allowlisted /
   scanner) for **both** classes: offending part absent, message + other parts delivered.
3. Per-class **difference**: e.g. `application/pdf` allowed as `real` but stripped as `inline`,
   and vice-versa; distinct `max_bytes` enforced per class.
4. **Back-compat:** `attachment_policy=None` → over-cap still raises → DLQ; existing
   attachment tests remain green untouched.
5. `connect(attachments=AttachmentPolicy(...))` and `connect(attachments={...})` produce
   identical behavior (dict coercion).
6. Recording: stripped parts increment the counter / emit the trace with correct reason+class.
7. Wire invariant: `EmailEvent.schema_version == "1.2"` after a strip.

## 8. Documentation

Rewrite the `examples/feature-explorer-v2.html` **"Configuring attachments"** section to show
the first-class `connect(attachments=AttachmentPolicy(real=…, inline=…))` API and the
inline/real split + strip-and-deliver semantics, replacing the current
"not exposed yet / build a `Pipeline` directly" caveat. Keep the fail-behavior warning,
updated for strip-vs-DLQ (opt-in).

## 9. Frozen contracts honored

- **Extractor seam** (`extract_bytes(...)`) unchanged; the policy is a constructor concern.
- **DLQ counting** untouched: a strip is not a dead-letter and must not call `add_dead_letter()`
  or emit a `dead_lettered` trace.
- **Schema:** no bump (`1.2` retained).
- **Back-compat:** default path (`attachments=None`) is byte-for-byte the current behavior.

## 10. Out of scope (follow-ups)

- `build_from_config` / YAML support for the JSON-able knobs (`max_bytes`, `allowlist`).
- Consumer-facing `stripped_attachments` field on `CleanEmail` (+ schema `1.3`).
- A real (non-no-op) scanner implementation.
