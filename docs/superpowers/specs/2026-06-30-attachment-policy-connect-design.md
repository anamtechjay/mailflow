# Per-class Attachment Policy through `connect()` — Design

**Date:** 2026-06-30
**Status:** Approved (design, rev 2 after independent review); implementation plan to follow.
**Scope:** Expose attachment size/type/scanner policy through the programmatic `connect()`
API, with **separate configuration for inline media vs. real attachments**, and an opt-in
**strip-and-deliver** disposition (the offending part is removed and recorded, the message is
delivered) instead of dead-lettering.

> **Rev 2 changelog** (independent review, 2026-06-30): the original "record via RunReport,
> no schema bump" plan was unimplementable for the connect()-only audience — the extractor
> returns only `CleanEmail` and `connect()` discards the `RunReport`, so a stripped part was
> invisible (silent data loss). Recording now lands on a new `CleanEmail.stripped_attachments`
> field (**schema 1.2 → 1.3**). Also fixed: inline classification for named-CID media, the
> dual-mechanism footgun, undecodable-part disposition, and the simple-case ergonomics.

---

## 1. Problem

The attachment safety knobs already exist on `MimeExtractor.__init__`
(`scanner`, `allowlist`, `max_attachment_bytes`), but every `connect()` path and
`build_from_config` hardcode a bare `MimeExtractor()` with defaults, so a user **cannot
configure attachments through the public API** today. Two further gaps:

- There is **one** policy for all non-body parts. Inline media (logos, tracking pixels,
  CID images) and real attachments (`Content-Disposition: attachment`) cannot be governed
  separately.
- The only failure mode is **fail-closed → DLQ**: a single over-cap or disallowed part
  dead-letters the *entire* message — undesirable for, e.g., an oversized inline pixel.

## 2. Decisions (locked during brainstorming; amended in rev 2)

| # | Decision |
|---|----------|
| D1 | **Disposition when configured:** strip the offending part (inline *or* real), record it, deliver the message; never dead-letter for attachment policy. Includes undecodable parts (rev 2, see §4). |
| D2 | **Opt-in:** with no attachment config, behavior is **unchanged** — over-cap/blocked/undecodable parts still raise → DLQ (existing tests + the "attachment cap" doc chip stay true). Strip-and-deliver activates only when policy is supplied. |
| D3 | **Scope:** programmatic `connect()` only. `build_from_config` (YAML/JSON) is a deliberate follow-up; the scanner is a live Python object and cannot live in a config file. |
| D4 | **API shape:** typed pydantic models `AttachmentPolicy` + `AttachmentRule`; `connect()` also accepts an equivalent plain `dict`. A **bare** `AttachmentRule`/dict (no `real`/`inline` keys) applies to both classes (rev 2). |
| D5 | **Recording (rev 2):** a stripped part is recorded on the delivered email via a new `CleanEmail.stripped_attachments: list[StrippedAttachment]` field (metadata only — no bytes). This **bumps `SCHEMA_VERSION` 1.2 → 1.3** (an additive optional field = minor bump, per `events.py`). The pipeline reads this field to emit observability traces/counter. Rationale: the connect() surface returns `CleanEmail`/projected dicts and discards the `RunReport`, so a consumer-visible field is the only channel that actually reaches this feature's audience. |

## 3. Public API

New exports from `mailflow` (`__init__.py`): `AttachmentPolicy`, `AttachmentRule`,
`StrippedAttachment`, `StripReason`.

```python
class AttachmentRule(BaseModel):
    max_bytes: int = 25_000_000                       # > 0 (validated)
    allowlist: frozenset[str] = frozenset()           # content-type OR lowercased ext; ∅ = allow all
    scanner: AttachmentScanner | None = None          # arbitrary_types_allowed; None = no-op (allow all)

class AttachmentPolicy(BaseModel):
    real:   AttachmentRule = AttachmentRule()         # real attachments (see §4.1 classification)
    inline: AttachmentRule = AttachmentRule()         # inline / CID media
```

`connect()` gains one keyword:

```python
def connect(..., attachments: AttachmentPolicy | AttachmentRule | dict[str, Any] | None = None) -> Mailflow
```

Normalization (in `facade.connect`, mirroring the existing `normalize_filters` polymorphic
idiom at `facade.py:92-104`):
- `None` (default) → today's behavior, fully unchanged.
- `AttachmentPolicy` → used directly.
- `AttachmentRule` → applied to **both** classes (`AttachmentPolicy(real=r, inline=r)`).
- `dict` **with** a `real`/`inline` key → `AttachmentPolicy.model_validate`.
- `dict` **without** `real`/`inline` (a bare rule, e.g. `{"max_bytes":…, "allowlist":[…]}`) →
  `AttachmentRule.model_validate` applied to both classes.

This restores the simple case — "max 10 MB, pdf only, everywhere":

```python
mf = connect("gmail", credentials=creds, mailbox="me",
             attachments={"max_bytes": 10_000_000, "allowlist": ["application/pdf"]})
```

…and the per-class case:

```python
from mailflow import connect, AttachmentPolicy, AttachmentRule
mf = connect("gmail", credentials=creds, mailbox="me",
    attachments=AttachmentPolicy(
        real   = AttachmentRule(max_bytes=10_000_000, allowlist={"application/pdf"}, scanner=AvScanner()),
        inline = AttachmentRule(max_bytes=1_000_000,  allowlist={"image/png", "image/jpeg"}),
    ))
```

**Loud note (rev 2, M5):** supplying a policy opts the **whole message's attachments** into
strip-mode. A class you do not configure defaults to allow-all / 25 MB **but still under
strip-mode** — i.e. tightening only `real` also switches `inline` from the global
fail-closed→DLQ posture to strip-and-deliver (a *more lenient*, never-DLQ posture). This is by
design (D1) and will be stated prominently in the docs (§8).

## 4. Extractor seam

`MimeExtractor.__init__` gains an optional `attachment_policy: AttachmentPolicy | None = None`,
**mutually exclusive** with the legacy single-rule params (rev 2, M3):

```python
def __init__(self, *, scanner=None, allowlist=DEFAULT_ALLOWLIST,
             max_attachment_bytes=MAX_ATTACHMENT_BYTES,
             attachment_policy: AttachmentPolicy | None = None) -> None:
    if attachment_policy is not None and (scanner is not None
            or allowlist != DEFAULT_ALLOWLIST or max_attachment_bytes != MAX_ATTACHMENT_BYTES):
        raise ValueError("pass attachment_policy OR the legacy scanner/allowlist/"
                         "max_attachment_bytes, not both")
```

### 4.1 Policy class selection (rev 2, M2 — the inline/real split)

The model field `Attachment.is_inline = is_inline_media and not is_attachment` is **not**
suitable for selecting the policy class: a named CID logo
(`Content-Type: image/png; name="logo.png"` + `Content-ID`, **no** `Content-Disposition`)
yields `is_attachment=True` (filename present, disp≠inline) and would be governed by the
`real` rule — silently defeating per-class config for the most common inline image.

Policy class is selected on **intent** instead:

```python
inline_for_policy = is_inline_media and disp != "attachment"
rule = policy.inline if inline_for_policy else policy.real
```

- A part with a `Content-ID` or `Content-Disposition: inline`, *not* explicitly
  `Content-Disposition: attachment` → **inline** rule (fixes named-CID logos).
- A part that is *both* (`Content-Disposition: attachment` + `Content-ID`) → **real** rule
  (documented; the explicit attachment disposition wins).

### 4.2 Dispatch

- **`attachment_policy is None`** → existing path: single rule; a violation **raises**
  (`AttachmentTooLargeError` / `AttachmentBlockedError` / `AttachmentUnreadableError`) → DLQ.
  **No behavior change.**
- **`attachment_policy` set** → choose `rule` per §4.1, apply in the existing cheap-first order:
  1. allowlist check on metadata (before decode) → on block: strip, reason `not_allowlisted`;
  2. decode + streaming cap at `rule.max_bytes` → on `AttachmentTooLargeError`: strip,
     reason `oversize`; on `AttachmentUnreadableError`: strip, reason `unreadable` (rev 2, M4);
  3. `rule.scanner.scan(meta)` → on block: strip, reason `scanner`.
  To **strip**: do not append to `attachments`, store nothing; append a `StrippedAttachment`
  record (filename, content_type, is_inline, reason; size when known) to a list returned with
  the `CleanEmail`. Never raise. The body and all compliant parts are delivered.

Notes: the streaming cap already aborts mid-stream without buffering, and blobs are written
only after a part passes all three checks, so a stripped part never leaves a partial blob.
`StripReason ∈ {not_allowlisted, oversize, unreadable, scanner}`.

## 5. Wiring

Replace the bare `MimeExtractor()` with a policy-aware one in the three `connect()` paths:

| Path | File:line (today) | Ownership |
|------|-------------------|-----------|
| memory pipeline | `facade.py:264` | pipeline-engineer |
| gmail fetch-by-id | `facade.py:327` (`_build_gmail_fetcher`) | pipeline-engineer |
| gmail live | `adapters/gmail/composition.py:60` (built for `run_service`) | adapters-engineer |

`connect()` normalizes `attachments` → `AttachmentPolicy | None`, threads it into all three.
The gmail live path threads the policy through `_build_gmail_live` → `run_service` →
`composition`. **Cross-ownership:** touches `core/models.py` + `core/events.py`
(field + schema bump), `core/__init__` exports, `extract/`, `facade.py`, `adapters/gmail/`,
and `core/observability.py` — the plan sequences commits so the tree stays importable at each
step (mypy strict, whole-tree). Commit a module before its importers.

## 6. Recording stripped parts (rev 2)

- The **extractor** populates `CleanEmail.stripped_attachments` (it already builds and returns
  the `CleanEmail`; the frozen `extract_bytes(...) -> CleanEmail` signature is unchanged).
- The **pipeline** (which owns the `RunReport`) reads `email.stripped_attachments` after
  extraction and emits an observability trace + increments a counter
  (e.g. `attachments_stripped`) — operators get metrics, consumers get the field. This keeps
  the recording out of the extractor↔RunReport boundary entirely.
- DLQ counting is untouched: a strip never calls `_dead_letter` / `add_dead_letter()` and never
  emits a `dead_lettered` trace.

## 7. Testing (TDD)

1. `AttachmentRule` validation: `max_bytes <= 0` rejected; `allowlist` list→frozenset;
   `AttachmentPolicy.model_validate(dict)` and `AttachmentRule.model_validate(dict)` round-trip.
2. Normalization: bare `AttachmentRule` and bare dict apply to **both** classes; a dict with
   `real`/`inline` keys builds a per-class policy.
3. Strip behavior, policy set, one test per `StripReason` (not_allowlisted / oversize /
   unreadable / scanner) for **both** classes: offending part absent, recorded in
   `stripped_attachments`, message + other parts delivered, no partial blob.
4. **Classification (M2):** a named-CID inline image (`name=…` + `Content-ID`, no disposition)
   is governed by the **inline** rule, not `real`; a `disposition=attachment` + `Content-ID`
   part is governed by `real`.
5. Per-class **difference:** `application/pdf` allowed as `real` but stripped as `inline`, and
   distinct `max_bytes` enforced per class.
6. **Mutual exclusion (M3):** `MimeExtractor(allowlist=…, attachment_policy=…)` raises
   `ValueError`.
7. **Back-compat (D2):** `attachment_policy=None` → over-cap/blocked/undecodable still raise →
   DLQ; existing attachment tests remain green untouched.
8. `connect(attachments=AttachmentPolicy(...))`, `connect(attachments=AttachmentRule(...))`, and
   `connect(attachments={...})` produce the expected behavior.
9. Recording: stripped parts populate `stripped_attachments`; pipeline emits the trace/counter.
10. Wire: `EmailEvent.schema_version == "1.3"`; a no-strip message has `stripped_attachments == []`.

## 8. Documentation

Rewrite the `examples/feature-explorer-v2.html` **"Configuring attachments"** section to show
the first-class `connect(attachments=…)` API (bare simple-case + per-class `AttachmentPolicy`),
the inline/real split + classification caveat (named-CID → inline), and strip-and-deliver
semantics with the loud opt-in note (§3). Update the "attachment cap" always-on chip tooltip:
default cap is DLQ; a policy switches that message to strip-and-record. Note schema is now 1.3.

## 9. Frozen contracts honored

- **Extractor seam** (`extract_bytes(...) -> CleanEmail`) signature unchanged; the policy is a
  constructor concern and `stripped_attachments` is a field on the existing return type.
- **DLQ counting** untouched: a strip is not a dead-letter (verified: `observability.py:76-91`
  — `record()` does not count `dead_lettered`, only `add_dead_letter()` does).
- **Schema:** `1.2 → 1.3` (additive optional field; consistent with the 1.1→1.2 precedent for
  `is_bounce`/`is_auto_submitted`).
- **Back-compat:** default path (`attachments=None`) is byte-for-byte the current behavior,
  including undecodable/over-cap → DLQ.

## 10. Out of scope (follow-ups)

- `build_from_config` / YAML support for the JSON-able knobs (`max_bytes`, `allowlist`). The
  capability gap (config-file users still get defaults) is acknowledged.
- A real (non-no-op) scanner implementation.
- Fixing `Attachment.is_inline` itself (kept as-is for the wire; policy uses the §4.1 selector).
