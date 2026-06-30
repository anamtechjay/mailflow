# Message Classification & Filtering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Ship two P1 message-classification / filtering capabilities for mailflow:
(1) built-in `ToFilter` / `CcFilter` deterministic filters (exact-address **and** regex modes, mirroring `SubjectFilter`), registered and config-wireable exactly like the existing filters; and
(2) a **thin** boolean classification seam — derived `is_auto_submitted: bool` and a thin-heuristic `is_bounce: bool` — added alongside the existing `auto_submitted` string field on `Envelope` and `CleanEmail`, with `ListMailFilter` rewired onto the boolean. The full RFC3464 DSN engine stays explicitly P2.

**Architecture:** Both features slot into the **already-frozen** Filter/model seams — no new ports, no pipeline changes.
- Filters implement the `Filter` Protocol (`name: str` + `evaluate(env, ctx) -> FilterDecision`) and drop-on-match like `SubjectFilter`/`BlockSenderFilter`; empty config returns `UNCERTAIN` (safe no-op) per the module's destructive-filter doctrine. They run on the cheap `Envelope` inside `FilterChain` (first KEEP/DROP wins).
- The boolean seam is **stored fields** (not `@computed_field`) so they appear in `CleanEmail.model_fields` (projectable via `fields=`), serialize onto the wire model, and keep the models "pure data". Derivation is centralized in one new pure helper module `core/classification.py` so the two envelope parsers and two extractors share a single definition (wire consistency). The `Envelope` copy feeds filters pre-extraction; the `CleanEmail` copy is the emitted wire value.
- Adding two optional fields to the wire model (`CleanEmail`) is a **minor** schema bump per `core/events.py` policy → `SCHEMA_VERSION "1.1" → "1.2"`.

**Tech Stack:** Python 3.12, pydantic 2.13, pytest 9, mypy strict. Interpreter is the venv: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python`. PyYAML is NOT installed (JSON-only config fixtures). Run tests with `… -m pytest`, types with `… -m mypy`.

---

## Conventions for every task

- **Test runner:** `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest <path> -q`
- **Type gate (must be clean before every commit):** `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
- Strict TDD: write the failing test → run it → confirm it fails for the stated reason → minimal real impl → green → `mypy` clean → commit.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Never `git push`.** Commit only.
- mypy-strict gotchas in play: the `**{"from": x}` splat is already hoisted to a `dict[str, Any]`-typed `alias_from`/`env_kwargs`/`ce_kwargs` var at every call site — **add new kwargs alongside that var or as explicit keyword args, never re-introduce an inline `**{...}` splat**. Narrow `str | None` with truthiness (`x and x.…`), not `bool(x) and x.…`, before calling string methods.

---

## TASK 1 — Built-in `ToFilter` / `CcFilter` (classes + unit tests)

**Files:**
- `src/mailflow/filters/deterministic.py` (owner: extract-filter-engineer) — add a module-level `_recipient_match` helper next to the existing `_domain` helper (after line 14), and two filter classes after `SubjectFilter` (after line 122). Widen the existing import `from mailflow.core.models import Envelope` (line 10) to also import `Recipient`.
- `tests/test_to_cc_filters.py` (NEW; owner: this driver) — unit tests.

- [ ] **Step 1: Write the failing unit tests.** Create `tests/test_to_cc_filters.py` with the unit half:

```python
"""Built-in To/Cc filters: exact-address and regex matching, drop-on-match, empty=no-op."""

from __future__ import annotations

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Decision, Envelope, Recipient, StreamRef
from mailflow.filters.deterministic import CcFilter, ToFilter

CTX = FilterContext(tenant="t")
STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _env(to: tuple[str, ...] = (), cc: tuple[str, ...] = ()) -> Envelope:
    return Envelope(
        canonical_id="c", provider="memory", provider_message_id="m",
        stream=STREAM, from_=Recipient(address="a@x.com"),
        to=[Recipient(address=a) for a in to],
        cc=[Recipient(address=a) for a in cc],
    )


def test_to_filter_exact_address_drops() -> None:
    f = ToFilter(addresses={"alerts@acme.com"})
    assert f.evaluate(_env(to=("alerts@acme.com",)), CTX).decision is Decision.drop
    assert f.evaluate(_env(to=("real@acme.com",)), CTX).decision is Decision.uncertain


def test_to_filter_case_insensitive() -> None:
    f = ToFilter(addresses={"Alerts@Acme.com"})
    assert f.evaluate(_env(to=("ALERTS@acme.COM",)), CTX).decision is Decision.drop


def test_to_filter_regex_pattern_drops() -> None:
    f = ToFilter(patterns=[r"(?i)^noreply@"])
    assert f.evaluate(_env(to=("noreply@vendor.io",)), CTX).decision is Decision.drop
    assert f.evaluate(_env(to=("sales@vendor.io",)), CTX).decision is Decision.uncertain


def test_to_filter_empty_is_safe_noop() -> None:
    assert ToFilter().evaluate(_env(to=("anyone@acme.com",)), CTX).decision is Decision.uncertain


def test_cc_filter_matches_cc_not_to() -> None:
    f = CcFilter(addresses={"list@acme.com"})
    assert f.evaluate(_env(cc=("list@acme.com",)), CTX).decision is Decision.drop
    # an address present only in To must NOT trip the Cc filter
    assert f.evaluate(_env(to=("list@acme.com",)), CTX).decision is Decision.uncertain


def test_cc_filter_regex() -> None:
    f = CcFilter(patterns=[r"@bulk\.example$"])
    assert f.evaluate(_env(cc=("x@bulk.example",)), CTX).decision is Decision.drop
    assert f.evaluate(_env(cc=("x@real.example",)), CTX).decision is Decision.uncertain
```

- [ ] **Step 2: Run it; confirm the expected failure.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_to_cc_filters.py -q`
  Expected: `ImportError: cannot import name 'CcFilter' from 'mailflow.filters.deterministic'` (collection error). That is the correct red.

- [ ] **Step 3: Minimal real implementation.** In `src/mailflow/filters/deterministic.py`, change the import line 10 to:

```python
from mailflow.core.models import Envelope, Recipient
```

Add the helper after `_domain` (after line 14):

```python
def _recipient_match(
    recipients: list[Recipient],
    addresses: set[str],
    patterns: list[re.Pattern[str]],
) -> str | None:
    """Reason string if any recipient matches an exact address (case-insensitive) or a
    regex, else None. Shared by ToFilter/CcFilter; mirrors SubjectFilter's regex search."""
    for r in recipients:
        addr = r.address.lower()
        if addr in addresses:
            return f"address {addr}"
        for pat in patterns:
            if pat.search(r.address):
                return f"~ {pat.pattern}"
    return None
```

Add the two classes after `SubjectFilter` (after line 122):

```python
class ToFilter:
    """Drops mail addressed TO a matching recipient — exact address (case-insensitive)
    or regex pattern, mirroring SubjectFilter's regex style. Empty config = no-op (safe)."""

    name = "to"

    def __init__(
        self, addresses: set[str] | None = None, patterns: list[str] | None = None
    ) -> None:
        self.addresses = {a.lower() for a in (addresses or set())}
        self.patterns = [re.compile(p) for p in (patterns or [])]

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if not self.addresses and not self.patterns:
            return FilterDecision.uncertain()                 # empty -> safe no-op
        reason = _recipient_match(env.to, self.addresses, self.patterns)
        if reason is not None:
            return FilterDecision.drop(self.name, reason)
        return FilterDecision.uncertain()


class CcFilter:
    """Cc counterpart of ToFilter — matches against the Cc list. Empty config = no-op (safe)."""

    name = "cc"

    def __init__(
        self, addresses: set[str] | None = None, patterns: list[str] | None = None
    ) -> None:
        self.addresses = {a.lower() for a in (addresses or set())}
        self.patterns = [re.compile(p) for p in (patterns or [])]

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if not self.addresses and not self.patterns:
            return FilterDecision.uncertain()
        reason = _recipient_match(env.cc, self.addresses, self.patterns)
        if reason is not None:
            return FilterDecision.drop(self.name, reason)
        return FilterDecision.uncertain()
```

- [ ] **Step 4: Green + types.**
  `… -m pytest tests/test_to_cc_filters.py -q` → all green.
  `… -m mypy` → `Success: no issues found`.

- [ ] **Step 5: Commit.**
  `feat(filters): built-in ToFilter + CcFilter (exact-address + regex, drop-on-match)`

---

## TASK 2 — Register `to`/`cc` + export + config end-to-end

**Files:**
- `src/mailflow/registry.py` (owner: pipeline-engineer) — import the two classes (after line 22 in the `deterministic` import block), add `"to", "cc"` to `FILTER_KINDS` (line 35–38), add two branches to `build_filter` (after the `subject` branch, ~line 55).
- `src/mailflow/filters/__init__.py` (owner: extract-filter-engineer) — import + `__all__` export.
- `tests/test_to_cc_filters.py` (append the end-to-end half).

> Note: `facade.normalize_filters` already routes a `{"kind": ...}` dict through `build_filter`, accepting both flat (`{"kind":"to","addresses":[...]}`) and nested (`{"kind":"cc","params":{...}}`) params — **no facade change is needed**; the e2e tests below assert both shapes.

- [ ] **Step 1: Write the failing e2e tests.** Append to `tests/test_to_cc_filters.py`:

```python
# ---- end-to-end through connect() (config dict spec; flat + nested params) ----

from mailflow import connect  # noqa: E402
from mailflow.providers.memory import SeedEmail  # noqa: E402


def _raw(mid: str, to: str, cc: str = "") -> bytes:
    cc_line = f"Cc: {cc}\r\n" if cc else ""
    return (f"Message-ID: <{mid}@x>\r\nFrom: s@x.com\r\nTo: {to}\r\n{cc_line}"
            f"Subject: hi\r\n\r\nbody").encode()


def test_to_filter_e2e_drops_matching_recipient() -> None:
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", "alerts@acme.com")),
        SeedEmail("m2", _raw("m2", "real@acme.com")),
    ]}
    mf = connect("memory", seed=seed, tenant="acme",
                 filters=[{"kind": "to", "addresses": ["alerts@acme.com"]}])
    out = mf.fetch_new()
    assert {r.address for e in out for r in e.to} == {"real@acme.com"}


def test_cc_filter_e2e_regex_nested_params() -> None:
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", "real@acme.com", cc="bulk@lists.io")),
        SeedEmail("m2", _raw("m2", "real@acme.com", cc="ok@acme.com")),
    ]}
    mf = connect("memory", seed=seed, tenant="acme",
                 filters=[{"kind": "cc", "params": {"patterns": ["@lists\\.io$"]}}])
    out = mf.fetch_new()
    assert len(out) == 1
    assert out[0].cc[0].address == "ok@acme.com"
```

- [ ] **Step 2: Run; confirm the expected failure.**
  `… -m pytest tests/test_to_cc_filters.py -k e2e -q`
  Expected red: the dict spec routes to `build_filter("to", …)` which raises `ValueError: unknown filter kind 'to'` (and `validate()` would reject the kind too).

- [ ] **Step 3: Minimal real implementation.**
  In `src/mailflow/registry.py`, extend the `deterministic` import to include the new names:

```python
from mailflow.filters.deterministic import (
    BlacklistFilter,
    BlockSenderFilter,
    CcFilter,
    InternalDomainFilter,
    ListMailFilter,
    NoPersonalFilter,
    OnlyDomainFilter,
    OnlySenderFilter,
    SubjectFilter,
    ToFilter,
    WhitelistFilter,
)
```

  Add `"to", "cc"` to `FILTER_KINDS`:

```python
FILTER_KINDS = {
    "whitelist", "blacklist", "internal_domain", "subject", "list_mail", "no_personal",
    "only_domain", "only_sender", "block_sender", "to", "cc",
}
```

  Add branches in `build_filter` (after the `subject` branch):

```python
    if kind == "to":
        return ToFilter(
            addresses=set(params.get("addresses", [])),
            patterns=list(params.get("patterns", [])),
        )
    if kind == "cc":
        return CcFilter(
            addresses=set(params.get("addresses", [])),
            patterns=list(params.get("patterns", [])),
        )
```

  In `src/mailflow/filters/__init__.py`, add `CcFilter` and `ToFilter` to both the `deterministic` import block and `__all__`.

- [ ] **Step 4: Green + types + no regressions.**
  `… -m pytest tests/test_to_cc_filters.py -q` → green.
  `… -m pytest -q` (full suite) → green (confirms registry/loader/facade still consistent).
  `… -m mypy` → clean.

- [ ] **Step 5: Commit.**
  `feat(registry): wire to/cc filter kinds + export ToFilter/CcFilter`

---

## TASK 3 — `core/classification.py` thin derivation helpers

**Files:**
- `src/mailflow/core/classification.py` (NEW; conceptually core-foundation-engineer's domain — pure, no vendor imports, no I/O). See CONTRACT DECISION about this new core module.
- `tests/core/test_classification.py` (NEW).

- [ ] **Step 1: Write the failing tests.** Create `tests/core/test_classification.py`:

```python
"""Thin auto-submitted / bounce classification helpers (the boolean seam, RFC3464 is P2)."""

from __future__ import annotations

from mailflow.core.classification import derive_auto_submitted, derive_is_bounce


def test_auto_submitted_present_not_no_is_true() -> None:
    assert derive_auto_submitted("auto-generated") is True
    assert derive_auto_submitted("auto-replied") is True


def test_auto_submitted_no_or_absent_is_false() -> None:
    assert derive_auto_submitted("no") is False
    assert derive_auto_submitted("No") is False
    assert derive_auto_submitted(None) is False
    assert derive_auto_submitted("") is False


def test_bounce_daemon_sender() -> None:
    assert derive_is_bounce(from_address="MAILER-DAEMON@mx.example",
                            return_path=None, content_type=None) is True
    assert derive_is_bounce(from_address="postmaster@mx.example",
                            return_path=None, content_type=None) is True


def test_bounce_null_return_path() -> None:
    assert derive_is_bounce(from_address="a@x.com", return_path="<>", content_type=None) is True
    assert derive_is_bounce(from_address="a@x.com", return_path="", content_type=None) is True


def test_bounce_delivery_status_report() -> None:
    ct = "multipart/report; report-type=delivery-status; boundary=xyz"
    assert derive_is_bounce(from_address="bounce@mx.example",
                            return_path="<bounce@mx.example>", content_type=ct) is True


def test_not_bounce_ordinary_mail() -> None:
    assert derive_is_bounce(from_address="alice@x.com",
                            return_path="<alice@x.com>",
                            content_type="text/plain") is False
    # a multipart/report read-receipt (disposition-notification) is NOT a bounce
    assert derive_is_bounce(
        from_address="alice@x.com", return_path="<alice@x.com>",
        content_type="multipart/report; report-type=disposition-notification") is False
```

- [ ] **Step 2: Run; confirm the failure.**
  `… -m pytest tests/core/test_classification.py -q`
  Expected: `ModuleNotFoundError: No module named 'mailflow.core.classification'`.

- [ ] **Step 3: Minimal real implementation.** Create `src/mailflow/core/classification.py`:

```python
"""Thin message-classification helpers — the boolean seam (§7.4 / R-D3).

Derive cheap boolean signals from already-parsed header values. Deliberately THIN:
the full RFC3464 DSN engine is a P2 concern. Keeping the logic here (one pure module,
no I/O, no vendor imports) lets the MIME + Graph envelope parsers and both extractors
share one definition so the wire stays consistent.
"""

from __future__ import annotations

# Local-parts that conventionally originate auto-generated bounce / failure mail.
_BOUNCE_SENDERS = {"mailer-daemon", "postmaster"}


def derive_auto_submitted(auto_submitted: str | None) -> bool:
    """True when an Auto-Submitted header is present and is not the literal "no"
    (RFC 3834). Mirrors the legacy `auto_submitted.lower() != "no"` filter check."""
    return bool(auto_submitted and auto_submitted.strip().lower() != "no")


def derive_is_bounce(
    *,
    from_address: str,
    return_path: str | None,
    content_type: str | None,
) -> bool:
    """Thin bounce heuristic (seam only — full RFC3464 parsing is P2). True when ANY:
      * the sender local-part is a daemon address (mailer-daemon / postmaster),
      * the Return-Path is the null path (`<>` or empty), or
      * the Content-Type is a delivery-status report
        (multipart/report; report-type=delivery-status).
    """
    local = from_address.split("@", 1)[0].strip().lower()
    if local in _BOUNCE_SENDERS:
        return True
    if return_path is not None and return_path.strip() in ("", "<>"):
        return True
    ct = (content_type or "").lower()
    if "multipart/report" in ct and "delivery-status" in ct:
        return True
    return False
```

- [ ] **Step 4: Green + types.**
  `… -m pytest tests/core/test_classification.py -q` → green. `… -m mypy` → clean.

- [ ] **Step 5: Commit.**
  `feat(core): thin classification seam helpers (derive_auto_submitted / derive_is_bounce)`

---

## TASK 4 — Seam fields on the models + `SCHEMA_VERSION` minor bump

**Files:**
- `src/mailflow/core/models.py` (owner: core-foundation-engineer) — add `is_auto_submitted: bool = False` and `is_bounce: bool = False` to `Envelope` (after line 138, `auto_submitted`) and to `CleanEmail` (after line 181, `auto_submitted`).
- `src/mailflow/core/events.py` (owner: core-foundation-engineer) — `SCHEMA_VERSION = "1.2"` (line 14).
- `tests/core/test_event_contract.py` — update the version assertion (line 18–19).
- `tests/core/test_models_contract.py` (extend with default-value assertions).

> The string `auto_submitted` field is **kept** (Graph extractor + downstream may read it); we only **add** the booleans. Adding two optional fields (default `False`) to the wire model `CleanEmail` is a minor bump (`1.1 → 1.2`); tolerant readers ignore unknown fields.

- [ ] **Step 1: Write the failing tests.**
  In `tests/core/test_event_contract.py` rename + update:

```python
def test_schema_version_minor_bumped_to_1_2() -> None:
    assert SCHEMA_VERSION == "1.2"
```

  Append to `tests/core/test_models_contract.py`:

```python
def test_clean_email_classification_seam_defaults_false() -> None:
    e = _email()
    assert e.is_auto_submitted is False
    assert e.is_bounce is False


def test_envelope_classification_seam_defaults_false() -> None:
    from mailflow.core.models import Envelope, StreamRef
    env = Envelope(canonical_id="c", provider="memory", provider_message_id="m",
                   stream=StreamRef(mailbox="ops@acme.com"))
    assert env.is_auto_submitted is False
    assert env.is_bounce is False
```

- [ ] **Step 2: Run; confirm the failures.**
  `… -m pytest tests/core/test_event_contract.py tests/core/test_models_contract.py -q`
  Expected reds: the new contract test asserts `"1.2"` while the constant is still `"1.1"`; the model tests `AttributeError: 'CleanEmail' object has no attribute 'is_auto_submitted'`.
  Also confirm no *other* test pins `"1.1"`: `grep -rn '"1\.1"' tests/` should now return only the (about-to-change) event-contract line.

- [ ] **Step 3: Minimal real implementation.**
  In `src/mailflow/core/models.py`, `Envelope` — replace the `auto_submitted` line:

```python
    auto_submitted: str | None = None
    is_auto_submitted: bool = False
    is_bounce: bool = False
```

  In `CleanEmail`, replace its `auto_submitted` line with the same three lines (keeping the surrounding `list_id` / `list_unsubscribe` lines intact).
  In `src/mailflow/core/events.py`:

```python
SCHEMA_VERSION = "1.2"
```

- [ ] **Step 4: Green + types + full suite.**
  `… -m pytest -q` (full suite — guards that no integration/golden test pinned the emitted `schema_version` to `"1.1"`).
  `… -m mypy` → clean.

- [ ] **Step 5: Commit.**
  `feat(model): add is_auto_submitted/is_bounce seam fields; bump SCHEMA_VERSION 1.1->1.2`

---

## TASK 5 — Populate the seam on `Envelope` + rewire `ListMailFilter`

**Files:**
- `src/mailflow/extract/envelope.py` (owner: extract-filter-engineer) — `MimeEnvelopeParser.parse_envelope` (build block lines 39–69).
- `src/mailflow/adapters/graph/parser.py` (owner: adapters-engineer) — `GraphEnvelopeParser.parse_envelope` `env_kwargs` (lines 56–77).
- `src/mailflow/filters/deterministic.py` (owner: extract-filter-engineer) — `ListMailFilter.evaluate` (line 131).
- `tests/test_bounce_seam.py` (NEW) and `tests/providers/graph/test_classification_seam.py` (NEW).

- [ ] **Step 1: Write the failing tests.** Create `tests/test_bounce_seam.py`:

```python
"""is_auto_submitted / is_bounce seam on the Envelope (MIME path) + ListMailFilter rewire."""

from __future__ import annotations

from datetime import datetime, timezone

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Cursor, Decision, Envelope, RawMessage, StreamRef
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.filters.deterministic import ListMailFilter

CTX = FilterContext(tenant="t")
STREAM = StreamRef(mailbox="ops@acme.com")


def _raw_msg(raw: bytes) -> RawMessage:
    return RawMessage(
        provider="memory", provider_message_id="m1", stream=STREAM,
        size_bytes=len(raw), received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        cursor=Cursor(value="1", order=1), raw_bytes=raw,
    )


def test_envelope_auto_submitted_seam() -> None:
    raw = (b"Message-ID: <a@x>\r\nFrom: s@x.com\r\nTo: ops@acme.com\r\n"
           b"Auto-Submitted: auto-generated\r\nSubject: hi\r\n\r\nbody")
    env = MimeEnvelopeParser().parse_envelope(_raw_msg(raw), "t")
    assert env.auto_submitted == "auto-generated"   # legacy string preserved
    assert env.is_auto_submitted is True
    assert env.is_bounce is False


def test_envelope_bounce_from_daemon_and_null_return_path() -> None:
    raw = (b"Message-ID: <a@x>\r\nFrom: MAILER-DAEMON@mx.acme.com\r\n"
           b"Return-Path: <>\r\nTo: ops@acme.com\r\nSubject: failure\r\n\r\nx")
    env = MimeEnvelopeParser().parse_envelope(_raw_msg(raw), "t")
    assert env.is_bounce is True


def test_list_mail_filter_uses_boolean_seam() -> None:
    drop_env = Envelope(canonical_id="c", provider="memory", provider_message_id="m",
                        stream=STREAM, is_auto_submitted=True)
    keep_env = Envelope(canonical_id="c", provider="memory", provider_message_id="m",
                        stream=STREAM, is_auto_submitted=False)
    assert ListMailFilter().evaluate(drop_env, CTX).decision is Decision.drop
    assert ListMailFilter().evaluate(keep_env, CTX).decision is Decision.uncertain
```

  Create `tests/providers/graph/test_classification_seam.py`:

```python
"""Graph envelope parser populates the is_auto_submitted / is_bounce seam."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.core.models import Cursor, RawMessage, StreamRef

STREAM = StreamRef(mailbox="ops@acme.com")


def _msg(data: dict) -> RawMessage:
    raw = json.dumps(data).encode()
    return RawMessage(
        provider="graph", provider_message_id="m1", stream=STREAM,
        size_bytes=len(raw), received_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        cursor=Cursor(value="1", order=1), raw_bytes=raw,
    )


def test_graph_envelope_auto_submitted_and_bounce() -> None:
    data = {
        "from": {"emailAddress": {"address": "mailer-daemon@mx.example"}},
        "internetMessageHeaders": [
            {"name": "Auto-Submitted", "value": "auto-replied"},
        ],
    }
    env = GraphEnvelopeParser().parse_envelope(_msg(data), "t")
    assert env.is_auto_submitted is True
    assert env.is_bounce is True   # daemon sender


def test_graph_envelope_ordinary_mail_not_classified() -> None:
    data = {"from": {"emailAddress": {"address": "alice@partner.com"}}}
    env = GraphEnvelopeParser().parse_envelope(_msg(data), "t")
    assert env.is_auto_submitted is False
    assert env.is_bounce is False
```

- [ ] **Step 2: Run; confirm the failures.**
  `… -m pytest tests/test_bounce_seam.py tests/providers/graph/test_classification_seam.py -q`
  Expected reds: `is_auto_submitted`/`is_bounce` remain `False` from the parsers (fields exist but are not yet populated), so the True-expecting asserts fail. `test_list_mail_filter_uses_boolean_seam` fails because `ListMailFilter` still reads `env.auto_submitted` (which is `None` here) so the `is_auto_submitted=True` envelope is NOT dropped.

- [ ] **Step 3: Minimal real implementation.**
  In `src/mailflow/extract/envelope.py`, add the import:

```python
from mailflow.core.classification import derive_auto_submitted, derive_is_bounce
```

  In `parse_envelope`, just before the `return Envelope(...)`, hoist the header reads, then pass the new kwargs (placed right after the existing `auto_submitted=` kwarg, before `headers=`):

```python
        auto_sub = str(parsed["auto-submitted"]) if parsed["auto-submitted"] else None
        content_type = str(parsed["content-type"]) if parsed["content-type"] is not None else None
        return_path = str(parsed["return-path"]) if parsed["return-path"] is not None else None
        from_addr = from_[0].address if from_ else ""
```

  and in the `Envelope(...)` call replace `auto_submitted=str(parsed["auto-submitted"]) ...` with:

```python
            auto_submitted=auto_sub,
            is_auto_submitted=derive_auto_submitted(auto_sub),
            is_bounce=derive_is_bounce(
                from_address=from_addr, return_path=return_path, content_type=content_type
            ),
```

  In `src/mailflow/adapters/graph/parser.py`, add the import:

```python
from mailflow.core.classification import derive_auto_submitted, derive_is_bounce
```

  In `parse_envelope`, hoist the `from` node and auto-submitted read, then add the kwargs to `env_kwargs` (replace the inline `"from": _recipient(data.get("from"))` and `"auto_submitted": _first(headers, "auto-submitted")` entries):

```python
        from_node = _recipient(data.get("from"))
        auto_sub = _first(headers, "auto-submitted")
        env_kwargs: dict[str, Any] = {
            ...
            "from": from_node,
            ...
            "auto_submitted": auto_sub,
            "is_auto_submitted": derive_auto_submitted(auto_sub),
            "is_bounce": derive_is_bounce(
                from_address=from_node.address,
                return_path=_first(headers, "return-path"),
                content_type=_first(headers, "content-type"),
            ),
            "headers": headers,
        }
```

  In `src/mailflow/filters/deterministic.py`, rewire `ListMailFilter.evaluate` (line 131):

```python
        if env.list_id or env.is_auto_submitted:
            return FilterDecision.drop(self.name, "list/auto-submitted header present")
```

- [ ] **Step 4: Green + types + full suite.**
  `… -m pytest tests/test_bounce_seam.py tests/providers/graph/test_classification_seam.py -q` → green.
  `… -m pytest -q` (full suite — confirms the `list_mail` filter behavior is unchanged for real list/auto-submitted mail). `… -m mypy` → clean.

- [ ] **Step 5: Commit.**
  `feat(extract): populate is_auto_submitted/is_bounce on Envelope; rewire ListMailFilter`

---

## TASK 6 — Populate the seam on `CleanEmail` (both extractors)

**Files:**
- `src/mailflow/extract/mime.py` (owner: extract-filter-engineer) — `MimeExtractor.extract_bytes` (build block, lines 84–120).
- `src/mailflow/adapters/graph/extractor.py` (owner: adapters-engineer) — `GraphExtractor.extract` `ce_kwargs` (lines 71–103); it already copies `auto_submitted` from `env`, so copy the booleans from `env` too.
- `tests/test_bounce_seam.py` (append `CleanEmail` cases) and `tests/providers/graph/test_classification_seam.py` (append a `GraphExtractor` case).

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_bounce_seam.py`:

```python
from mailflow.extract.mime import MimeExtractor  # noqa: E402


def test_clean_email_bounce_delivery_status_report() -> None:
    raw = (b"Message-ID: <a@x>\r\nFrom: bounce-handler@mailer.acme.com\r\n"
           b"To: ops@acme.com\r\nSubject: Delivery Status\r\n"
           b'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n'
           b"\r\n--b\r\nContent-Type: text/plain\r\n\r\nfailed\r\n--b--\r\n")
    ce = MimeExtractor().extract_bytes(
        raw, provider="memory", provider_message_id="m1",
        stream_id="ops@acme.com", watched_mailbox="ops@acme.com",
    )
    assert ce.is_bounce is True            # via the delivery-status content-type
    assert ce.is_auto_submitted is False


def test_clean_email_auto_submitted_seam() -> None:
    raw = (b"Message-ID: <a@x>\r\nFrom: s@x.com\r\nTo: ops@acme.com\r\n"
           b"Auto-Submitted: auto-generated\r\nSubject: hi\r\n\r\nbody")
    ce = MimeExtractor().extract_bytes(
        raw, provider="memory", provider_message_id="m1",
        stream_id="ops@acme.com", watched_mailbox="ops@acme.com",
    )
    assert ce.is_auto_submitted is True
    assert ce.auto_submitted == "auto-generated"   # legacy string preserved
```

  Append to `tests/providers/graph/test_classification_seam.py`:

```python
def test_graph_extractor_copies_seam_from_envelope() -> None:
    from mailflow.adapters.graph.extractor import GraphExtractor
    data = {
        "from": {"emailAddress": {"address": "mailer-daemon@mx.example"}},
        "internetMessageHeaders": [{"name": "Auto-Submitted", "value": "auto-replied"}],
    }
    msg = _msg(data)
    env = GraphEnvelopeParser().parse_envelope(msg, "t")
    ce = GraphExtractor().extract(msg, env)
    assert ce.is_auto_submitted is True
    assert ce.is_bounce is True
```

- [ ] **Step 2: Run; confirm the failures.**
  `… -m pytest tests/test_bounce_seam.py -k clean_email tests/providers/graph/test_classification_seam.py -k extractor -q`
  Expected reds: the new `CleanEmail` asserts get the default `False` because neither extractor populates the booleans yet.

- [ ] **Step 3: Minimal real implementation.**
  In `src/mailflow/extract/mime.py`, add the import:

```python
from mailflow.core.classification import derive_auto_submitted, derive_is_bounce
```

  In `extract_bytes`, before the `return CleanEmail(...)`, hoist the header reads:

```python
        auto_sub = str(msg["auto-submitted"]) if msg["auto-submitted"] else None
        content_type = str(msg["content-type"]) if msg["content-type"] is not None else None
        return_path = str(msg["return-path"]) if msg["return-path"] is not None else None
```

  and in the `CleanEmail(...)` call replace `auto_submitted=str(msg["auto-submitted"]) ...` with:

```python
            auto_submitted=auto_sub,
            is_auto_submitted=derive_auto_submitted(auto_sub),
            is_bounce=derive_is_bounce(
                from_address=from_.address, return_path=return_path, content_type=content_type
            ),
```

  In `src/mailflow/adapters/graph/extractor.py`, add to `ce_kwargs` right after the existing `"auto_submitted": env.auto_submitted,` entry:

```python
            "is_auto_submitted": env.is_auto_submitted,
            "is_bounce": env.is_bounce,
```

- [ ] **Step 4: Green + types + full suite.**
  `… -m pytest -q` (full suite). `… -m mypy` → clean.

- [ ] **Step 5: Commit.**
  `feat(extract): populate is_auto_submitted/is_bounce on CleanEmail (MIME + Graph)`

---

## Final verification pass

- [ ] **Full green + clean types:**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q`
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
- [ ] **No stray `"1.1"` schema pins remain:** `grep -rn '"1\.1"' src/ tests/` returns nothing.
- [ ] **Seam is consistent across all four sites:** `grep -rn "is_auto_submitted\|is_bounce" src/` shows population in `extract/mime.py`, `extract/envelope.py`, `adapters/graph/parser.py`, `adapters/graph/extractor.py`, field declarations in `core/models.py`, derivation in `core/classification.py`, and the rewired `ListMailFilter` in `filters/deterministic.py`.
- [ ] **`git log --oneline`** shows the six feature commits in order.

---

## CONTRACT DECISIONS (must be in the executor's final report)

1. **SCHEMA_VERSION bumped `1.1 → 1.2`** (`core/events.py`). Justification: two optional fields (`is_auto_submitted`, `is_bounce`, both default `False`) are added to the wire model `CleanEmail`; the events.py docstring policy classifies "adding an optional field" as a minor bump. Backward compatible — tolerant readers ignore unknown fields. The contract test `tests/core/test_event_contract.py` is updated accordingly. Downstream impact: any consumer asserting the literal `"1.1"` must update; none found in-repo besides that test.

2. **`is_bounce` heuristic boundary (THIN, by design — RFC3464 is P2).** `is_bounce` is `True` iff ANY of: (a) the sender local-part is `mailer-daemon` **or** `postmaster` (both standard bounce originators — `postmaster` added beyond the prompt's `Mailer-Daemon` example as the RFC-standard counterpart), (b) the `Return-Path` is the null path (`<>` or empty), or (c) the `Content-Type` is `multipart/report; report-type=delivery-status`. Explicitly **out of scope** (deferred to P2): parsing the RFC3464 `message/delivery-status` body, `X-Failed-Recipients`, per-recipient status codes, `disposition-notification` read-receipts (deliberately classified as NOT a bounce). Downstream impact: classifiers/filters may read `is_bounce` as a cheap signal but must not assume DSN-grade accuracy.

3. **`auto_submitted` string field retained.** The boolean `is_auto_submitted` is added **alongside** the legacy `auto_submitted: str | None`, not replacing it (the Graph extractor and potential consumers still read the raw string). Only `ListMailFilter` is rewired from `env.auto_submitted.lower() != "no"` to `env.is_auto_submitted` — behavior-equivalent.

4. **Stored fields, not `@computed_field`.** The booleans are real stored fields (default `False`) rather than computed properties, so they (a) appear in `CleanEmail.model_fields` and are therefore selectable via the facade's `fields=[...]` projection (`make_projection` iterates `model_fields`), and (b) keep models "pure data". Derivation is centralized in the new pure module `core/classification.py`.

5. **New core module `core/classification.py`.** Conceptually core-foundation-engineer's domain (pure, no vendor imports). It is not in the original ownership table's explicit file list for that role; the lead should assign it there. It introduces no import cycles (depends on nothing in `mailflow`).

## Exported names downstream teammates depend on

- `mailflow.filters.deterministic.ToFilter` / `CcFilter` (also re-exported from `mailflow.filters`); registry kinds `"to"` / `"cc"` with params `addresses: list[str]` and `patterns: list[str]` (both optional; empty = safe no-op; drop-on-match).
- `mailflow.core.classification.derive_auto_submitted(auto_submitted) -> bool` and `derive_is_bounce(*, from_address, return_path, content_type) -> bool`.
- New fields `Envelope.is_auto_submitted: bool`, `Envelope.is_bounce: bool`, `CleanEmail.is_auto_submitted: bool`, `CleanEmail.is_bounce: bool` (defaults `False`).
- `SCHEMA_VERSION == "1.2"`.
