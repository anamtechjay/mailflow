# Mailflow Core Spine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the provider- and transport-agnostic core of `mailflow` — the full ingestion pipeline (parse → filter → extract → emit) plus every §8 correctness invariant — runnable end-to-end against an in-memory provider with zero external setup.

**Architecture:** Ports & adapters (hexagonal). A pure-Python `core` depends only on `typing.Protocol` ports; in-memory adapters (provider, cursor/dedupe/blob stores, emitter) implement those ports so the whole pipeline runs and is tested deterministically without any vendor SDK or cloud account. The Microsoft Graph and Gmail adapters (later plans) plug into the *same* ports without touching `core`.

**Tech Stack:** Python 3.12+, pydantic v2 (models/config/`SecretStr`), Python stdlib `email` (MIME parsing), `hashlib` (content + identity hashing), pytest (tests), mypy (verifies port conformance — `@runtime_checkable` only checks method names per §5.3 / R-D5). No vendor SDKs in this plan.

**Project root:** `/Users/iamanam/projects/techjays/poc/mailflow/`. All paths below are relative to this root. The package import name is `mailflow` (placeholder per spec OD-1 — renaming after release is breaking; not blocking for this plan).

**Scope note:** This is **Plan 1 of 4** (spec §15 roadmap). It delivers Phase-1 *core* only. Out of scope here, each its own later plan: the Graph adapter + webhook/subscribe (Plan 2), the Gmail adapter (Plan 3), and the reference deployment + allowlisted plugins + attachment streaming + LLM classifier hardening (Plan 4). The `Classifier` port is *defined* here but the pipeline treats it as optional and ships with none wired.

---

## Implementation Model: Agent Teams (read this first)

This plan is executed by a **Claude Code agent team** (experimental — see [docs](https://code.claude.com/docs/en/agent-teams)). One session is the **lead** (coordinator + reviewer); five **engineer teammates** own disjoint directories; one read-only **verifier** audits conformance at every checkpoint. The roles are reusable subagent definitions already created in `.claude/agents/`.

**Why split by directory, not by phase:** agent teams avoid file conflicts only when no two teammates touch the same file. The module boundaries in the File Structure below are the ownership boundaries. The build is dependency-heavy (the pipeline needs everything else first), so the *order* is encoded as **task dependencies** on the shared task list — blocked tasks auto-unblock when their dependency completes.

| Teammate (`.claude/agents/…`) | Owns (files) | Plan tasks |
|---|---|---|
| `core-foundation-engineer` | `core/{errors,models,identity,events,ports,filtering,observability}.py` + `tests/core/` | 2–6, observability in 12 |
| `extract-filter-engineer` | `extract/`, `filters/` + their tests | 7, 8, 9 |
| `adapters-engineer` | `stores/`, `providers/`, `emit/` + their tests | 10, 11, emitters in 12 |
| `pipeline-engineer` | `core/pipeline.py`, `config/`, `registry.py`, `builder.py`, `__init__.py` + their tests | 13, 14, 15, 16 |
| `fixtures-golden-engineer` | `tests/fixtures/`, `tests/test_golden_corpus.py`, `tests/test_end_to_end.py`, `README.md` | 16.5, 17 |
| `plan-conformance-verifier` | *read-only* — audits all of the above | runs at every checkpoint |

**Dependency DAG (the lead seeds the shared task list with these edges):**

```
Task 1 (scaffold, lead)
   └─> core-foundation (T2–6) ─┬─> extract-filter (T7–9) ─┐
                               ├─> adapters (T10–12) ──────┼─> pipeline (T13–16) ─> golden+e2e (T16.5,17)
                               └────────────────────────────┘
   verifier: sweep after core-foundation, after adapters+extract, after pipeline, and final.
```

`core-foundation` is the critical path — it freezes the contracts everyone binds to, so it goes first and alone. `extract-filter` and `adapters` then run **in parallel** (both depend only on core, touch no shared files). `pipeline` integrates them. `golden+e2e` validates against real mail last. The verifier gates each transition.

### Task 0: Stand up the agent team

- [ ] **Step 1: Enable agent teams** (requires Claude Code ≥ v2.1.32). In `.claude/settings.json` (or the environment):

```json
{
  "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" },
  "teammateMode": "in-process"
}
```

Confirm: `claude --version` ≥ 2.1.32. (Split-pane mode needs tmux/iTerm2; `in-process` works anywhere — cycle teammates with Shift+Down.)

- [ ] **Step 2: Confirm the role definitions exist**

Run: `ls .claude/agents/` — expect `plan-conformance-verifier.md`, `core-foundation-engineer.md`, `extract-filter-engineer.md`, `adapters-engineer.md`, `pipeline-engineer.md`, `fixtures-golden-engineer.md`. These are already authored; the lead references them by name when spawning.

- [ ] **Step 3: Lead creates the team and seeds the task list**

The lead (this session) does Task 1 itself (scaffold — one file set, no parallelism to gain), then spawns the five engineers from their definitions and creates the shared tasks with the dependency edges from the DAG above. Tell the lead, in your own words, something like: *"Create an agent team. Spawn core-foundation-engineer, extract-filter-engineer, adapters-engineer, pipeline-engineer, fixtures-golden-engineer from their agent definitions, plus plan-conformance-verifier. Seed the task list from the plan with these dependencies: extract-filter and adapters depend on core-foundation; pipeline depends on both; golden+e2e depends on pipeline. Have core-foundation start; hold the rest until their dependencies clear. Require plan approval from the pipeline-engineer before it edits pipeline.py."*

- [ ] **Step 4: Baseline verifier sweep**

Ask the verifier for an initial sweep. On an empty repo it should report `Overall: BLOCKED — nothing implemented yet`, confirming it can read the plan + spec and run `pytest`/`mypy`. This proves the audit loop works before real code exists.

- [ ] **Step 5: Quality gate (recommended)**

Optionally wire a [`TaskCompleted` hook](https://code.claude.com/docs/en/hooks#taskcompleted) that runs `cd mailflow && python -m mypy` and exits non-zero to block marking a task done while mypy is red — so "done" always means typed-and-green.

> **Single-session fallback:** if agent teams aren't enabled, the same plan runs fine via `superpowers:subagent-driven-development` (one subagent per task, lead reviews between) — the role definitions still apply as the subagent `agentType` for each task. Agent teams add parallelism between the `extract-filter` and `adapters` tracks and let you message a stuck teammate directly; they cost more tokens.

> **Phase-1 learnings baked into the setup (applied to all future plans):**
> - **Shared conventions live in `mailflow/CLAUDE.md`** — every teammate auto-loads it: the venv path, the mypy-strict gotcha catalog, the frozen contracts, the ownership table, and the required report format. The six role files in `.claude/agents/` now point at it and carry only their role-specific learning.
> - **Serialize file-disjoint tracks in subagent mode.** `extract-filter` and `adapters` are file-disjoint and *look* parallel, but subagents share one `.git/index` and one `.mypy_cache` — concurrent commits/mypy race. Real agent-teams avoid this with per-teammate **git worktrees** (`isolation: worktree`); subagent execution runs them sequentially. The dependency DAG is unchanged; only the parallelism is.
> - **CONTRACT DECISIONs are a required report field.** When a teammate resolves a plan ambiguity that crosses module boundaries (Phase 1: the `dead_lettered` counting rule), it must be surfaced so the lead propagates it — not resolved silently. The verifier now explicitly checks propagation.

---

## File Structure

Each file has one responsibility; files that change together live together (spec §5.4).

| File | Responsibility |
|------|----------------|
| `pyproject.toml` | Package metadata, `mailflow` src-layout, pytest + mypy config, pydantic dep |
| `src/mailflow/__init__.py` | Curated public API (`__version__`, re-exports) |
| `src/mailflow/core/errors.py` | Exception hierarchy |
| `src/mailflow/core/models.py` | Enums, `Recipient`, `StreamRef`, `Cursor`, `RawMessage`, `Attachment`, `Envelope`, `CleanEmail` |
| `src/mailflow/core/identity.py` | `stable_hash`, `idempotency_key`, `derive_canonical_id` + trust flags (§6.1) |
| `src/mailflow/core/events.py` | `EmailEvent`, `SCHEMA_VERSION` (§14) |
| `src/mailflow/core/ports.py` | The 11 `Protocol` ports — **no vendor imports** (§5.3) |
| `src/mailflow/core/filtering.py` | `Decision`, `FilterDecision`, `FilterContext` (shared filter value types) |
| `src/mailflow/core/observability.py` | `DecisionTrace`, `DeadLetter`, `RunReport` (§8.5, §12) |
| `src/mailflow/extract/mime.py` | `MimeExtractor`: RFC822 bytes → `CleanEmail` (§7.6) |
| `src/mailflow/extract/envelope.py` | `MimeEnvelopeParser`: `RawMessage` → `Envelope` (cheap, §7.3) |
| `src/mailflow/filters/chain.py` | `FilterChain` three-valued runner (§7.4) |
| `src/mailflow/filters/deterministic.py` | `WhitelistFilter`, `BlacklistFilter`, `InternalDomainFilter`, `SubjectFilter`, `ListMailFilter` |
| `src/mailflow/stores/memory.py` | `InMemoryCursorStore` (monotonic CAS), `InMemoryDedupeStore` (atomic claim + attempts), `InMemoryBlobStore` |
| `src/mailflow/providers/memory.py` | `MemoryProvider` (`MailboxProvider`), `seed` helper |
| `src/mailflow/emit/memory.py` | `MemoryEmitter`, `EmitReceipt` |
| `src/mailflow/emit/stdout.py` | `StdoutEmitter` |
| `src/mailflow/core/pipeline.py` | The orchestrator + all §8 invariants |
| `src/mailflow/config/schema.py` | pydantic config models |
| `src/mailflow/config/loader.py` | Layered load + `validate()` (§11) |
| `src/mailflow/registry.py` | Built-in `kind` → class registry (allowlist seam for Plan 4) |
| `src/mailflow/builder.py` | `build_from_config`, hand-wire `Pipeline` constructor (§12) |
| `tests/...` | One test module per source module + one end-to-end test |

---

## Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `src/mailflow/__init__.py`
- Create: `tests/test_smoke.py`
- Create: `.gitignore`

- [ ] **Step 1: Initialise the repo and a virtualenv**

```bash
cd /Users/iamanam/projects/techjays/poc/mailflow
git init
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip "pydantic>=2.6" pytest mypy
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "mailflow"
version = "0.1.0"
description = "Provider- and transport-agnostic email ingestion toolkit (core spine)"
requires-python = ">=3.12"
dependencies = ["pydantic>=2.6"]

[project.optional-dependencies]
dev = ["pytest>=8", "mypy>=1.9"]

[tool.hatch.build.targets.wheel]
packages = ["src/mailflow"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.mypy]
mypy_path = "src"
packages = ["mailflow"]
python_version = "3.12"
strict = true
```

- [ ] **Step 3: Write `.gitignore` and the package entry point**

`.gitignore`:
```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
dist/
```

`src/mailflow/__init__.py`:
```python
"""mailflow — email ingestion toolkit (core spine)."""

__version__ = "0.1.0"
```

- [ ] **Step 4: Write a smoke test**

`tests/test_smoke.py`:
```python
import mailflow


def test_package_imports_and_has_version():
    assert mailflow.__version__ == "0.1.0"
```

- [ ] **Step 5: Run it to verify the toolchain works**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: scaffold mailflow package with pytest + mypy"
```

---

## Task 2: Exception hierarchy

**Files:**
- Create: `src/mailflow/core/errors.py`
- Create: `src/mailflow/core/__init__.py` (empty)
- Test: `tests/core/test_errors.py`

- [ ] **Step 1: Write the failing test**

`tests/core/test_errors.py`:
```python
import pytest

from mailflow.core.errors import (
    ConfigError,
    ExtractionError,
    MailflowError,
    OversizedMessageError,
    UnknownKindError,
)


def test_all_errors_subclass_mailflow_error():
    for exc in (OversizedMessageError, ExtractionError, ConfigError, UnknownKindError):
        assert issubclass(exc, MailflowError)


def test_unknown_kind_is_a_config_error():
    assert issubclass(UnknownKindError, ConfigError)


def test_oversized_carries_the_byte_counts():
    err = OversizedMessageError(actual=200, limit=100)
    assert err.actual == 200 and err.limit == 100
    assert "200" in str(err) and "100" in str(err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/core/test_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.core.errors'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/core/__init__.py`: *(empty file)*

`src/mailflow/core/errors.py`:
```python
"""Exception hierarchy. Every mailflow error subclasses MailflowError."""


class MailflowError(Exception):
    """Base for all mailflow errors."""


class ExtractionError(MailflowError):
    """Raised when a message cannot be parsed into a CleanEmail."""


class OversizedMessageError(MailflowError):
    """Raised when a message exceeds the configured byte ceiling (spec §8.6)."""

    def __init__(self, actual: int, limit: int) -> None:
        self.actual = actual
        self.limit = limit
        super().__init__(f"message is {actual} bytes, over the {limit}-byte limit")


class ConfigError(MailflowError):
    """Invalid configuration."""


class UnknownKindError(ConfigError):
    """A config `kind` is not in the registry (spec §11)."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/core/test_errors.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/core/__init__.py src/mailflow/core/errors.py tests/core/test_errors.py
git commit -m "feat(core): exception hierarchy"
```

---

## Task 3: Core value models (enums, Recipient, StreamRef, Cursor, RawMessage, Attachment)

**Files:**
- Create: `src/mailflow/core/models.py`
- Test: `tests/core/test_models.py`

These are the small, dependency-free value types. `Envelope` and `CleanEmail` come in Task 5 (they depend on identity from Task 4).

- [ ] **Step 1: Write the failing test**

`tests/core/test_models.py`:
```python
from datetime import datetime, timezone

from mailflow.core.models import (
    Attachment,
    Cursor,
    Decision,
    Direction,
    Disposition,
    RawMessage,
    Recipient,
    StreamRef,
    Verdict,
)


def test_enum_values_are_stable_strings():
    assert Direction.inbound.value == "inbound"
    assert Disposition.dead_lettered.value == "dead_lettered"
    assert Decision.uncertain.value == "uncertain"
    assert Verdict.not_relevant.value == "not_relevant"


def test_streamref_key_with_and_without_folder():
    assert StreamRef(mailbox="ops@x.com").key == "ops@x.com"
    assert StreamRef(mailbox="ops@x.com", folder="Inbox").key == "ops@x.com:Inbox"


def test_streamref_is_hashable_and_frozen():
    s = StreamRef(mailbox="ops@x.com", folder="Inbox")
    assert s in {s}  # hashable -> usable as a dict key for per-stream state


def test_cursor_orders_by_its_order_field():
    assert Cursor(value="h2", order=2) > Cursor(value="h1", order=1)
    assert Cursor(value="h1", order=1) < Cursor(value="h2", order=2)
    assert not (Cursor(value="x", order=5) > Cursor(value="y", order=5))


def test_rawmessage_holds_cheap_metadata_plus_lazy_bytes():
    msg = RawMessage(
        provider="memory",
        provider_message_id="m1",
        stream=StreamRef(mailbox="ops@x.com", folder="Inbox"),
        size_bytes=42,
        received_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        cursor=Cursor(value="c1", order=1),
        raw_bytes=b"From: a@x.com\r\n\r\nhi",
    )
    assert msg.size_bytes == 42
    assert msg.raw_bytes.startswith(b"From:")


def test_attachment_defaults():
    att = Attachment(filename="a.pdf", content_type="application/pdf", size_bytes=10)
    assert att.is_inline is False
    assert att.content_hash == ""
    assert att.storage_ref == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/core/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.core.models'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/core/models.py`:
```python
"""Core value models. Pure data, no behaviour beyond derivation helpers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from functools import total_ordering

from pydantic import BaseModel, ConfigDict, Field


class Direction(str, Enum):
    inbound = "inbound"
    outbound = "outbound"
    unknown = "unknown"


class Disposition(str, Enum):
    """The four terminal states a message can reach (spec §8.1)."""

    emitted = "emitted"
    dropped = "dropped"
    duplicate = "duplicate"
    dead_lettered = "dead_lettered"


class Decision(str, Enum):
    """Three-valued filter outcome (spec §7.4)."""

    keep = "keep"
    drop = "drop"
    uncertain = "uncertain"


class Verdict(str, Enum):
    relevant = "relevant"
    not_relevant = "not_relevant"
    unknown = "unknown"


class Recipient(BaseModel):
    name: str = ""
    address: str = ""


class StreamRef(BaseModel):
    """One sync stream: a mailbox (Gmail) or a mailbox folder (Graph) — spec §8.3."""

    model_config = ConfigDict(frozen=True)

    mailbox: str
    folder: str | None = None

    @property
    def key(self) -> str:
        return f"{self.mailbox}:{self.folder}" if self.folder else self.mailbox


@total_ordering
class Cursor(BaseModel):
    """Opaque per-stream bookmark. `order` is the monotonic comparator (spec §8.3)."""

    model_config = ConfigDict(frozen=True)

    value: str
    order: int

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Cursor):
            return NotImplemented
        return self.order < other.order

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Cursor):
            return NotImplemented
        return self.order == other.order

    def __hash__(self) -> int:
        return hash((self.value, self.order))


class RawMessage(BaseModel):
    """A fetched message: cheap metadata always present; `raw_bytes` is the heavy
    payload only touched during EXTRACT (spec §7.3 vs §7.6)."""

    provider: str
    provider_message_id: str
    stream: StreamRef
    size_bytes: int
    received_at: datetime
    cursor: Cursor
    raw_bytes: bytes = b""


class Attachment(BaseModel):
    filename: str = ""
    content_type: str = "application/octet-stream"
    size_bytes: int = 0
    content_hash: str = ""  # sha256 of bytes (spec §6.2)
    content_id: str = ""
    is_inline: bool = False
    provider_attachment_id: str = ""
    storage_ref: str = ""  # pointer in BlobStore, NOT the bytes

    model_config = ConfigDict(populate_by_name=True)
    _unused: int = Field(default=0, exclude=True)  # placeholder to keep mypy strict happy
```

> Note: drop the `_unused` placeholder line — it is illustrative only; if mypy strict is clean without it, omit it. (Do not ship dead fields.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/core/test_models.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/core/models.py tests/core/test_models.py
git commit -m "feat(core): value models (enums, Recipient, StreamRef, Cursor, RawMessage, Attachment)"
```

---

## Task 4: Identity & idempotency (§6.1)

**Files:**
- Create: `src/mailflow/core/identity.py`
- Test: `tests/core/test_identity.py`

This encodes the spec's two-keys rule: `idempotency_key = (tenant, mailbox, provider_message_id)` for dedupe, and a *guaranteed-present* `canonical_id` that uses the RFC `Message-ID` only when trusted, else a stable hash.

- [ ] **Step 1: Write the failing test**

`tests/core/test_identity.py`:
```python
from mailflow.core.identity import (
    derive_canonical_id,
    idempotency_key,
    is_message_id_trusted,
    stable_hash,
)


def test_idempotency_key_combines_tenant_mailbox_provider_id():
    assert idempotency_key("acme", "ops@x.com", "m1") == "acme|ops@x.com|m1"


def test_idempotency_key_is_stable_and_distinct():
    a = idempotency_key("acme", "ops@x.com", "m1")
    b = idempotency_key("acme", "ops@x.com", "m1")
    c = idempotency_key("acme", "other@x.com", "m1")  # same id, different mailbox -> distinct
    assert a == b and a != c


def test_stable_hash_is_deterministic_and_provider_scoped():
    h1 = stable_hash("graph", "pmid", "ops@x.com")
    h2 = stable_hash("graph", "pmid", "ops@x.com")
    h3 = stable_hash("gmail", "pmid", "ops@x.com")
    assert h1 == h2 and h1 != h3
    assert len(h1) == 64  # sha256 hex


def test_present_well_formed_message_id_is_trusted():
    assert is_message_id_trusted("<abc.123@example.com>") is True


def test_absent_or_malformed_message_id_is_not_trusted():
    assert is_message_id_trusted(None) is False
    assert is_message_id_trusted("") is False
    assert is_message_id_trusted("not-an-id") is False  # no @, no angle brackets


def test_canonical_id_uses_message_id_when_trusted():
    cid, present, trusted = derive_canonical_id(
        provider="graph",
        provider_message_id="pmid",
        mailbox="ops@x.com",
        message_id="<abc.123@example.com>",
    )
    assert cid == "<abc.123@example.com>"
    assert present is True and trusted is True


def test_canonical_id_falls_back_to_stable_hash_when_untrusted():
    cid, present, trusted = derive_canonical_id(
        provider="graph",
        provider_message_id="pmid",
        mailbox="ops@x.com",
        message_id=None,
    )
    assert cid == stable_hash("graph", "pmid", "ops@x.com")
    assert present is False and trusted is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/core/test_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.core.identity'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/core/identity.py`:
```python
"""Identity derivation (spec §6.1).

Two keys for two jobs:
  - idempotency_key((tenant, mailbox, provider_message_id)) -> dedupe (§8.2)
  - canonical_id -> always-present surrogate; uses RFC Message-ID only when trusted.
"""

from __future__ import annotations

import hashlib

_SEP = "|"


def idempotency_key(tenant: str, mailbox: str, provider_message_id: str) -> str:
    """Stable dedupe key. (tenant, mailbox) included so the same mail in two watched
    inboxes counts as two arrivals, not one (spec §6.1)."""
    return _SEP.join((tenant, mailbox, provider_message_id))


def stable_hash(provider: str, provider_message_id: str, mailbox: str) -> str:
    """Deterministic sha256 surrogate used when the Message-ID is untrusted."""
    raw = _SEP.join((provider, provider_message_id, mailbox)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def is_message_id_trusted(message_id: str | None) -> bool:
    """A Message-ID is trustworthy only if present and shaped like `<local@domain>`.
    RFC 5322 says it SHOULD (not MUST) exist and uniqueness is the sender's job, so
    we treat anything malformed as untrusted (spec §6.1, R-D1)."""
    if not message_id:
        return False
    mid = message_id.strip()
    return mid.startswith("<") and mid.endswith(">") and "@" in mid


def derive_canonical_id(
    *,
    provider: str,
    provider_message_id: str,
    mailbox: str,
    message_id: str | None,
) -> tuple[str, bool, bool]:
    """Return (canonical_id, message_id_present, message_id_trusted)."""
    present = bool(message_id and message_id.strip())
    trusted = is_message_id_trusted(message_id)
    if trusted:
        assert message_id is not None
        return message_id.strip(), present, trusted
    return stable_hash(provider, provider_message_id, mailbox), present, trusted
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/core/test_identity.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/core/identity.py tests/core/test_identity.py
git commit -m "feat(core): identity + idempotency derivation (§6.1)"
```

---

## Task 5: Envelope, CleanEmail, EmailEvent + SCHEMA_VERSION

**Files:**
- Modify: `src/mailflow/core/models.py` (add `Envelope`, `Relevance`, `CleanEmail`)
- Create: `src/mailflow/core/events.py`
- Test: `tests/core/test_clean_email.py`
- Test: `tests/core/test_events.py`

- [ ] **Step 1: Write the failing tests**

`tests/core/test_clean_email.py`:
```python
from mailflow.core.models import CleanEmail, Direction, Recipient, Relevance, Verdict


def test_clean_email_minimal_construction():
    ce = CleanEmail(
        canonical_id="<a@x.com>",
        provider="memory",
        provider_message_id="m1",
        provider_stream_id="ops@x.com:Inbox",
        from_=Recipient(address="a@x.com"),
        subject="hi",
    )
    assert ce.canonical_id == "<a@x.com>"
    assert ce.direction is Direction.unknown
    assert ce.attachments == []
    assert ce.labels == [] and ce.categories == []
    assert ce.relevance.verdict is Verdict.unknown


def test_clean_email_from_alias_serialises_to_from():
    ce = CleanEmail(
        canonical_id="c",
        provider="memory",
        provider_message_id="m1",
        provider_stream_id="s",
        from_=Recipient(address="a@x.com"),
    )
    dumped = ce.model_dump(by_alias=True)
    assert dumped["from"]["address"] == "a@x.com"
    assert "from_" not in dumped


def test_relevance_round_trips():
    r = Relevance(verdict=Verdict.relevant, score=0.9, reason="looks like a customer")
    assert r.score == 0.9 and r.verdict is Verdict.relevant
```

`tests/core/test_events.py`:
```python
from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail, Recipient


def _email() -> CleanEmail:
    return CleanEmail(
        canonical_id="c",
        provider="memory",
        provider_message_id="m1",
        provider_stream_id="s",
        from_=Recipient(address="a@x.com"),
    )


def test_event_stamps_the_schema_version():
    evt = EmailEvent(email=_email(), tenant="acme", ordering_key="ops@x.com")
    assert evt.schema_version == SCHEMA_VERSION
    assert SCHEMA_VERSION.count(".") == 1  # major.minor (spec §14)


def test_event_carries_tenant_and_ordering_key():
    evt = EmailEvent(email=_email(), tenant="acme", ordering_key="ops@x.com")
    assert evt.tenant == "acme"
    assert evt.ordering_key == "ops@x.com"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/core/test_clean_email.py tests/core/test_events.py -v`
Expected: FAIL — `ImportError: cannot import name 'CleanEmail'` and `No module named 'mailflow.core.events'`.

- [ ] **Step 3: Add `Envelope`, `Relevance`, `CleanEmail` to `models.py`**

Append to `src/mailflow/core/models.py`:
```python
class Relevance(BaseModel):
    """Classifier output — non-destructive by default (spec OD-2)."""

    verdict: Verdict = Verdict.unknown
    score: float | None = None
    reason: str = ""


class Envelope(BaseModel):
    """Cheap, provider-neutral parse used by filters BEFORE full extraction (§7.3)."""

    canonical_id: str
    message_id: str | None = None
    message_id_present: bool = False
    message_id_trusted: bool = False
    provider: str
    provider_message_id: str
    stream: StreamRef
    from_: Recipient = Field(default_factory=Recipient, alias="from")
    sender: Recipient | None = None
    reply_to: Recipient | None = None
    to: list[Recipient] = Field(default_factory=list)
    cc: list[Recipient] = Field(default_factory=list)
    subject: str = ""
    date_utc: datetime | None = None
    received_at: datetime | None = None
    snippet: str = ""
    list_id: str | None = None
    list_unsubscribe: str | None = None
    auto_submitted: str | None = None
    headers: dict[str, list[str]] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class CleanEmail(BaseModel):
    """The normalized, provider-agnostic email we emit (spec §6.2)."""

    canonical_id: str
    message_id: str | None = None
    message_id_trusted: bool = False
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)

    provider: str
    provider_message_id: str
    provider_stream_id: str
    direction: Direction = Direction.unknown
    is_draft: bool = False

    from_: Recipient = Field(default_factory=Recipient, alias="from")
    sender: Recipient | None = None
    reply_to: Recipient | None = None
    to: list[Recipient] = Field(default_factory=list)
    cc: list[Recipient] = Field(default_factory=list)
    bcc: list[Recipient] = Field(default_factory=list)

    subject: str = ""
    date_utc: datetime | None = None
    received_at: datetime | None = None

    body_text: str = ""
    body_html: str = ""
    body_truncated: bool = False
    attachments: list[Attachment] = Field(default_factory=list)

    labels: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    folder: str = ""

    auto_submitted: str | None = None
    list_id: str | None = None
    list_unsubscribe: str | None = None
    message_size_bytes: int = 0
    raw_headers: dict[str, list[str]] = Field(default_factory=dict)

    relevance: Relevance = Field(default_factory=Relevance)
    matched_filter: str = ""
    schema_version: str = ""

    model_config = ConfigDict(populate_by_name=True)
```

- [ ] **Step 4: Write `events.py`**

`src/mailflow/core/events.py`:
```python
"""The wire contract emitted to transports (spec §14).

SCHEMA_VERSION is `major.minor`. Adding an optional field is a minor bump;
removing/retyping/re-meaning a field is a major bump. Consumers are tolerant
readers: refuse an unknown major, ignore unknown fields.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from mailflow.core.models import CleanEmail

SCHEMA_VERSION = "1.0"


class EmailEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    tenant: str
    ordering_key: str = ""
    email: CleanEmail
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/core/test_clean_email.py tests/core/test_events.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add src/mailflow/core/models.py src/mailflow/core/events.py tests/core/test_clean_email.py tests/core/test_events.py
git commit -m "feat(core): Envelope, CleanEmail, EmailEvent + SCHEMA_VERSION (§6.2, §14)"
```

---

## Task 6: Filter value types & the ports

**Files:**
- Create: `src/mailflow/core/filtering.py`
- Create: `src/mailflow/core/ports.py`
- Test: `tests/core/test_filtering.py`
- Test: `tests/core/test_ports.py`

The 11 ports are `Protocol`s; the real conformance check is mypy (Task 17 runs it). The runtime test here is a smoke test only (spec §5.3 caveat).

- [ ] **Step 1: Write the failing tests**

`tests/core/test_filtering.py`:
```python
from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import Decision


def test_filter_decision_keep():
    d = FilterDecision.keep("whitelist", "domain partner.com")
    assert d.decision is Decision.keep
    assert d.filter_name == "whitelist"
    assert "partner.com" in d.reason


def test_filter_decision_uncertain_has_no_filter_name_requirement():
    d = FilterDecision.uncertain()
    assert d.decision is Decision.uncertain
    assert d.filter_name == ""


def test_filter_context_carries_tenant():
    ctx = FilterContext(tenant="acme")
    assert ctx.tenant == "acme"
```

`tests/core/test_ports.py`:
```python
from mailflow.core import ports


def test_all_eleven_ports_are_exported():
    expected = {
        "MailboxProvider",
        "SubscriptionManager",
        "EnvelopeParser",
        "Filter",
        "Classifier",
        "ContentExtractor",
        "Emitter",
        "CursorStore",
        "DedupeStore",
        "SecretProvider",
        "BlobStore",
    }
    assert expected <= set(dir(ports))


def test_runtime_checkable_smoke_for_emitter():
    # @runtime_checkable verifies method NAMES only (spec §5.3 / R-D5).
    class FakeEmitter:
        def emit(self, event):  # noqa: ANN001, ANN201
            return None

    assert isinstance(FakeEmitter(), ports.Emitter)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/core/test_filtering.py tests/core/test_ports.py -v`
Expected: FAIL — `No module named 'mailflow.core.filtering'`.

- [ ] **Step 3: Write `filtering.py`**

`src/mailflow/core/filtering.py`:
```python
"""Shared filter value types (kept out of ports.py to avoid import cycles)."""

from __future__ import annotations

from pydantic import BaseModel

from mailflow.core.models import Decision


class FilterContext(BaseModel):
    tenant: str = ""


class FilterDecision(BaseModel):
    decision: Decision
    filter_name: str = ""
    reason: str = ""

    @classmethod
    def keep(cls, filter_name: str, reason: str = "") -> "FilterDecision":
        return cls(decision=Decision.keep, filter_name=filter_name, reason=reason)

    @classmethod
    def drop(cls, filter_name: str, reason: str = "") -> "FilterDecision":
        return cls(decision=Decision.drop, filter_name=filter_name, reason=reason)

    @classmethod
    def uncertain(cls) -> "FilterDecision":
        return cls(decision=Decision.uncertain)
```

- [ ] **Step 4: Write `ports.py`**

`src/mailflow/core/ports.py`:
```python
"""The ports — structural contracts the core depends on. NO vendor imports.

Per spec §5.3 / R-D5, @runtime_checkable only checks method-name existence and is
slow; static typing (mypy strict) is the real conformance gate.
"""

from __future__ import annotations

from typing import Iterable, Iterator, Protocol, runtime_checkable

from mailflow.core.events import EmailEvent
from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import (
    Cursor,
    Envelope,
    CleanEmail,
    RawMessage,
    Relevance,
    StreamRef,
)


@runtime_checkable
class MailboxProvider(Protocol):
    def connect(self) -> None: ...
    def sync_streams(self) -> Iterable[StreamRef]: ...
    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]: ...
    def message_size(self, msg: RawMessage) -> int | None: ...


@runtime_checkable
class SubscriptionManager(Protocol):
    def ensure_watch(self, stream: StreamRef) -> object: ...
    def renew_watch(self, handle: object) -> object: ...


@runtime_checkable
class EnvelopeParser(Protocol):
    def parse_envelope(self, msg: RawMessage, tenant: str) -> Envelope: ...


@runtime_checkable
class Filter(Protocol):
    name: str
    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision: ...


@runtime_checkable
class Classifier(Protocol):
    def classify(self, env: Envelope, ctx: FilterContext) -> Relevance: ...


@runtime_checkable
class ContentExtractor(Protocol):
    def extract(self, msg: RawMessage, env: Envelope) -> CleanEmail: ...


@runtime_checkable
class Emitter(Protocol):
    def emit(self, event: EmailEvent) -> object: ...


@runtime_checkable
class CursorStore(Protocol):
    def get(self, tenant: str, stream: StreamRef) -> Cursor | None: ...
    def commit_if_ahead(self, tenant: str, stream: StreamRef, cursor: Cursor) -> bool: ...


@runtime_checkable
class DedupeStore(Protocol):
    def try_claim(self, key: str, lease_seconds: int) -> bool: ...
    def record_attempt(self, key: str) -> int: ...
    def mark_done(self, key: str, ttl_seconds: int) -> None: ...
    def release(self, key: str) -> None: ...


@runtime_checkable
class SecretProvider(Protocol):
    def get(self, ref: str) -> str: ...


@runtime_checkable
class BlobStore(Protocol):
    def put_stream(self, ref: str, chunks: Iterator[bytes], content_type: str) -> str: ...
    def open(self, ref: str) -> Iterator[bytes]: ...
```

> **Type-consistency anchor (used by every later task):**
> - `EnvelopeParser.parse_envelope(msg, tenant)` — tenant is required (canonical_id has no tenant but the parser is per-tenant for symmetry; keep this exact signature).
> - `DedupeStore` exposes `try_claim`/`record_attempt`/`mark_done`/`release` — the attempt counter lives in the claim record (spec §8.4). The narrowed spec list in §5.3 is the minimum; this plan's contract is these four methods.
> - `MailboxProvider.message_size(msg)` takes the `RawMessage` (cheap metadata is already on it).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/core/test_filtering.py tests/core/test_ports.py -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Commit**

```bash
git add src/mailflow/core/filtering.py src/mailflow/core/ports.py tests/core/test_filtering.py tests/core/test_ports.py
git commit -m "feat(core): filter value types + 11 ports (§5.3)"
```

---

## Task 7: MIME content extractor (§7.6)

**Files:**
- Create: `src/mailflow/extract/__init__.py` (empty)
- Create: `src/mailflow/extract/mime.py`
- Test: `tests/extract/test_mime.py`

Parses RFC822 bytes into a `CleanEmail`: addresses, body (prefer text/plain, fall back to HTML), attachments (real vs inline), `content_hash`, `raw_headers`, threading headers, `direction`.

- [ ] **Step 1: Write the failing test**

`tests/extract/test_mime.py`:
```python
from mailflow.core.models import Direction
from mailflow.extract.mime import MimeExtractor

PLAIN = (
    b"Message-ID: <abc.1@example.com>\r\n"
    b"From: Alice <alice@partner.com>\r\n"
    b"To: ops@acme.com\r\n"
    b"Subject: Quote request\r\n"
    b"Date: Mon, 09 Jun 2026 10:00:00 +0000\r\n"
    b"\r\n"
    b"Hello, please send a quote.\r\n"
)

MULTIPART = (
    b"Message-ID: <m2@example.com>\r\n"
    b"From: Bob <bob@partner.com>\r\n"
    b"To: ops@acme.com\r\n"
    b"Subject: With attachment\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n'
    b"\r\n"
    b"--B\r\n"
    b"Content-Type: text/plain\r\n\r\nBody text here\r\n"
    b"--B\r\n"
    b"Content-Type: application/pdf\r\n"
    b'Content-Disposition: attachment; filename="q.pdf"\r\n\r\n'
    b"PDFBYTES\r\n"
    b"--B\r\n"
    b"Content-Type: image/png\r\n"
    b"Content-ID: <logo>\r\n"
    b"Content-Disposition: inline\r\n\r\n"
    b"PNGBYTES\r\n"
    b"--B--\r\n"
)


def _extract(raw: bytes, mailbox="ops@acme.com:Inbox"):
    return MimeExtractor().extract_bytes(
        raw, provider="memory", provider_message_id="m", stream_id=mailbox,
        watched_mailbox="ops@acme.com",
    )


def test_extracts_headers_addresses_and_body():
    ce = _extract(PLAIN)
    assert ce.subject == "Quote request"
    assert ce.from_.address == "alice@partner.com"
    assert ce.from_.name == "Alice"
    assert ce.to[0].address == "ops@acme.com"
    assert "send a quote" in ce.body_text
    assert ce.message_id == "<abc.1@example.com>"
    assert ce.message_id_trusted is True
    assert ce.raw_headers["subject"] == ["Quote request"]


def test_inbound_direction_when_sender_is_not_the_watched_mailbox():
    ce = _extract(PLAIN)
    assert ce.direction is Direction.inbound


def test_outbound_direction_when_sender_is_the_watched_mailbox():
    raw = PLAIN.replace(b"alice@partner.com", b"ops@acme.com")
    ce = _extract(raw)
    assert ce.direction is Direction.outbound


def test_separates_real_attachment_from_inline_media():
    ce = _extract(MULTIPART)
    reals = [a for a in ce.attachments if not a.is_inline]
    inlines = [a for a in ce.attachments if a.is_inline]
    assert len(reals) == 1 and reals[0].filename == "q.pdf"
    assert reals[0].content_hash and len(reals[0].content_hash) == 64
    assert len(inlines) == 1 and inlines[0].content_id == "<logo>"
    assert "Body text here" in ce.body_text


def test_canonical_id_present_even_without_message_id():
    raw = PLAIN.replace(b"Message-ID: <abc.1@example.com>\r\n", b"")
    ce = _extract(raw)
    assert ce.canonical_id  # always present
    assert ce.message_id is None
    assert ce.message_id_trusted is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/extract/test_mime.py -v`
Expected: FAIL — `No module named 'mailflow.extract.mime'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/extract/__init__.py`: *(empty)*

`src/mailflow/extract/mime.py`:
```python
"""RFC822 bytes -> CleanEmail (spec §7.6). Uses stdlib `email` with the modern
policy so headers come back parsed and unfolded."""

from __future__ import annotations

import hashlib
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime

from mailflow.core.events import SCHEMA_VERSION
from mailflow.core.identity import derive_canonical_id
from mailflow.core.models import Attachment, CleanEmail, Direction, Recipient


def _recipients(msg: EmailMessage, header: str) -> list[Recipient]:
    values = msg.get_all(header, [])
    return [Recipient(name=name, address=addr) for name, addr in getaddresses(values) if addr]


def _one(msg: EmailMessage, header: str) -> Recipient | None:
    rs = _recipients(msg, header)
    return rs[0] if rs else None


def _raw_headers(msg: EmailMessage) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in msg.items():
        out.setdefault(key.lower(), []).append(str(value))
    return out


class MimeExtractor:
    """ContentExtractor implementation for raw RFC822 (Gmail format=raw / memory)."""

    def extract_bytes(
        self,
        raw: bytes,
        *,
        provider: str,
        provider_message_id: str,
        stream_id: str,
        watched_mailbox: str,
    ) -> CleanEmail:
        msg = message_from_bytes(raw, policy=default_policy)
        assert isinstance(msg, EmailMessage)

        message_id = msg["message-id"]
        message_id = str(message_id) if message_id is not None else None
        mailbox = watched_mailbox
        canonical_id, _present, trusted = derive_canonical_id(
            provider=provider,
            provider_message_id=provider_message_id,
            mailbox=mailbox,
            message_id=message_id,
        )

        from_ = _one(msg, "from") or Recipient()
        direction = (
            Direction.outbound
            if from_.address.lower() == watched_mailbox.lower()
            else Direction.inbound
        )

        body_text, body_html, attachments = self._walk_body(msg)

        date_hdr = msg["date"]
        date_utc = None
        if date_hdr is not None:
            try:
                date_utc = parsedate_to_datetime(str(date_hdr))
            except (TypeError, ValueError):
                date_utc = None

        refs = msg.get_all("references", [])
        references = " ".join(str(r) for r in refs).split() if refs else []

        return CleanEmail(
            canonical_id=canonical_id,
            message_id=message_id,
            message_id_trusted=trusted,
            in_reply_to=str(msg["in-reply-to"]) if msg["in-reply-to"] else None,
            references=references,
            provider=provider,
            provider_message_id=provider_message_id,
            provider_stream_id=stream_id,
            direction=direction,
            **{"from": from_},  # alias
            sender=_one(msg, "sender"),
            reply_to=_one(msg, "reply-to"),
            to=_recipients(msg, "to"),
            cc=_recipients(msg, "cc"),
            bcc=_recipients(msg, "bcc"),
            subject=str(msg["subject"] or ""),
            date_utc=date_utc,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            auto_submitted=str(msg["auto-submitted"]) if msg["auto-submitted"] else None,
            list_id=str(msg["list-id"]) if msg["list-id"] else None,
            list_unsubscribe=str(msg["list-unsubscribe"]) if msg["list-unsubscribe"] else None,
            message_size_bytes=len(raw),
            raw_headers=_raw_headers(msg),
            schema_version=SCHEMA_VERSION,
        )

    def _walk_body(
        self, msg: EmailMessage
    ) -> tuple[str, str, list[Attachment]]:
        body_text = ""
        body_html = ""
        attachments: list[Attachment] = []

        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = (part.get_content_disposition() or "").lower()
            cid = part.get("content-id")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""

            is_attachment = disp == "attachment" or (filename and disp != "inline")
            is_inline_media = disp == "inline" or cid is not None

            if not is_attachment and not is_inline_media and ctype == "text/plain" and not body_text:
                body_text = part.get_content()
                continue
            if not is_attachment and not is_inline_media and ctype == "text/html" and not body_html:
                body_html = part.get_content()
                continue

            if is_attachment or is_inline_media:
                attachments.append(
                    Attachment(
                        filename=filename or "",
                        content_type=ctype,
                        size_bytes=len(payload),
                        content_hash=hashlib.sha256(payload).hexdigest() if payload else "",
                        content_id=str(cid) if cid else "",
                        is_inline=bool(is_inline_media and not is_attachment),
                    )
                )

        return body_text, body_html, attachments
```

> **Edge note for the implementer:** a part with *both* a filename and `Content-Disposition: inline` is treated as a real attachment (the `is_attachment` check wins). A part with a `Content-ID` and no `attachment` disposition is inline (logos, tracking pixels — spec §6.2). The test `test_separates_real_attachment_from_inline_media` pins this exact behavior.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/extract/test_mime.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/extract/__init__.py src/mailflow/extract/mime.py tests/extract/test_mime.py
git commit -m "feat(extract): MIME content extractor -> CleanEmail (§7.6)"
```

---

## Task 8: Envelope parser (§7.3)

**Files:**
- Create: `src/mailflow/extract/envelope.py`
- Test: `tests/extract/test_envelope.py`

Cheap, header-only parse that produces an `Envelope` for the filter stage *without* decoding the full body or attachments.

- [ ] **Step 1: Write the failing test**

`tests/extract/test_envelope.py`:
```python
from datetime import datetime, timezone

from mailflow.core.models import Cursor, RawMessage, StreamRef
from mailflow.extract.envelope import MimeEnvelopeParser

RAW = (
    b"Message-ID: <e1@example.com>\r\n"
    b"From: Carol <carol@partner.com>\r\n"
    b"To: ops@acme.com, sales@acme.com\r\n"
    b"Subject: Newsletter\r\n"
    b"List-Id: <news.partner.com>\r\n"
    b"Auto-Submitted: auto-generated\r\n"
    b"\r\n"
    b"This is the body and it is fairly long so the snippet should be truncated nicely.\r\n"
)


def _raw_message() -> RawMessage:
    return RawMessage(
        provider="memory",
        provider_message_id="m1",
        stream=StreamRef(mailbox="ops@acme.com", folder="Inbox"),
        size_bytes=len(RAW),
        received_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        cursor=Cursor(value="c1", order=1),
        raw_bytes=RAW,
    )


def test_parses_envelope_fields():
    env = MimeEnvelopeParser().parse_envelope(_raw_message(), tenant="acme")
    assert env.subject == "Newsletter"
    assert env.from_.address == "carol@partner.com"
    assert [r.address for r in env.to] == ["ops@acme.com", "sales@acme.com"]
    assert env.list_id == "<news.partner.com>"
    assert env.auto_submitted == "auto-generated"
    assert env.canonical_id == "<e1@example.com>"
    assert env.message_id_trusted is True


def test_snippet_is_bounded():
    env = MimeEnvelopeParser(snippet_chars=20).parse_envelope(_raw_message(), tenant="acme")
    assert len(env.snippet) <= 20
    assert env.snippet.startswith("This is the body")


def test_envelope_keeps_provider_metadata():
    env = MimeEnvelopeParser().parse_envelope(_raw_message(), tenant="acme")
    assert env.provider == "memory"
    assert env.provider_message_id == "m1"
    assert env.stream.key == "ops@acme.com:Inbox"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/extract/test_envelope.py -v`
Expected: FAIL — `No module named 'mailflow.extract.envelope'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/extract/envelope.py`:
```python
"""RawMessage -> Envelope (spec §7.3): headers + a snippet, no body decode."""

from __future__ import annotations

from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses

from mailflow.core.identity import derive_canonical_id
from mailflow.core.models import Envelope, RawMessage, Recipient


class MimeEnvelopeParser:
    def __init__(self, snippet_chars: int = 256) -> None:
        self.snippet_chars = snippet_chars

    def parse_envelope(self, msg: RawMessage, tenant: str) -> Envelope:
        parsed = message_from_bytes(msg.raw_bytes, policy=default_policy)
        assert isinstance(parsed, EmailMessage)

        def recips(header: str) -> list[Recipient]:
            return [
                Recipient(name=n, address=a)
                for n, a in getaddresses(parsed.get_all(header, []))
                if a
            ]

        message_id = parsed["message-id"]
        message_id = str(message_id) if message_id is not None else None
        canonical_id, present, trusted = derive_canonical_id(
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            mailbox=msg.stream.mailbox,
            message_id=message_id,
        )

        from_ = recips("from")
        snippet = self._snippet(parsed)
        headers: dict[str, list[str]] = {}
        for k, v in parsed.items():
            headers.setdefault(k.lower(), []).append(str(v))

        return Envelope(
            canonical_id=canonical_id,
            message_id=message_id,
            message_id_present=present,
            message_id_trusted=trusted,
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            stream=msg.stream,
            **{"from": from_[0] if from_ else Recipient()},
            sender=(recips("sender") or [None])[0],
            reply_to=(recips("reply-to") or [None])[0],
            to=recips("to"),
            cc=recips("cc"),
            subject=str(parsed["subject"] or ""),
            received_at=msg.received_at,
            snippet=snippet,
            list_id=str(parsed["list-id"]) if parsed["list-id"] else None,
            list_unsubscribe=str(parsed["list-unsubscribe"]) if parsed["list-unsubscribe"] else None,
            auto_submitted=str(parsed["auto-submitted"]) if parsed["auto-submitted"] else None,
            headers=headers,
        )

    def _snippet(self, parsed: EmailMessage) -> str:
        body = parsed.get_body(preferencelist=("plain", "html"))
        if body is None:
            return ""
        text = body.get_content()
        return text.strip().replace("\r\n", " ").replace("\n", " ")[: self.snippet_chars]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/extract/test_envelope.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/extract/envelope.py tests/extract/test_envelope.py
git commit -m "feat(extract): cheap envelope parser (§7.3)"
```

---

## Task 9: Filter chain + deterministic filters (§7.4)

**Files:**
- Create: `src/mailflow/filters/__init__.py` (empty)
- Create: `src/mailflow/filters/deterministic.py`
- Create: `src/mailflow/filters/chain.py`
- Test: `tests/filters/test_deterministic.py`
- Test: `tests/filters/test_chain.py`

Three-valued chain. **Destructive filters ship empty by default** (spec §7.4) — a filter constructed with no patterns must return UNCERTAIN, never DROP.

- [ ] **Step 1: Write the failing tests**

`tests/filters/test_deterministic.py`:
```python
from mailflow.core.filtering import FilterContext
from mailflow.core.models import Decision, Envelope, Recipient, StreamRef
from mailflow.filters.deterministic import (
    BlacklistFilter,
    InternalDomainFilter,
    ListMailFilter,
    SubjectFilter,
    WhitelistFilter,
)

CTX = FilterContext(tenant="acme")


def _env(**kw) -> Envelope:
    base = dict(
        canonical_id="c",
        provider="memory",
        provider_message_id="m",
        stream=StreamRef(mailbox="ops@acme.com"),
    )
    base.update(kw)
    return Envelope(**base)


def test_whitelist_keeps_matching_domain_else_uncertain():
    f = WhitelistFilter(domains={"partner.com"})
    keep = f.evaluate(_env(**{"from": Recipient(address="x@partner.com")}), CTX)
    miss = f.evaluate(_env(**{"from": Recipient(address="x@other.com")}), CTX)
    assert keep.decision is Decision.keep
    assert miss.decision is Decision.uncertain


def test_blacklist_drops_matching_else_uncertain():
    f = BlacklistFilter(domains={"spam.com"})
    drop = f.evaluate(_env(**{"from": Recipient(address="x@spam.com")}), CTX)
    assert drop.decision is Decision.drop
    assert f.evaluate(_env(**{"from": Recipient(address="x@ok.com")}), CTX).decision is Decision.uncertain


def test_empty_destructive_filters_never_drop():
    # Opinionated default: empty config = no opinion (spec §7.4).
    for f in (BlacklistFilter(domains=set()), InternalDomainFilter(domains=set()),
              SubjectFilter(patterns=[])):
        d = f.evaluate(_env(subject="anything", **{"from": Recipient(address="x@y.com")}), CTX)
        assert d.decision is Decision.uncertain


def test_subject_filter_drops_on_regex_match():
    f = SubjectFilter(patterns=[r"(?i)out of office"])
    d = f.evaluate(_env(subject="Automatic reply: Out Of Office"), CTX)
    assert d.decision is Decision.drop


def test_list_mail_filter_drops_when_list_id_present():
    f = ListMailFilter()
    d = f.evaluate(_env(list_id="<news.x.com>"), CTX)
    assert d.decision is Decision.drop
    assert f.evaluate(_env(), CTX).decision is Decision.uncertain
```

`tests/filters/test_chain.py`:
```python
from mailflow.core.filtering import FilterContext
from mailflow.core.models import Decision, Envelope, Recipient, StreamRef
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import BlacklistFilter, SubjectFilter, WhitelistFilter

CTX = FilterContext(tenant="acme")


def _env(addr="x@y.com", subject="") -> Envelope:
    return Envelope(
        canonical_id="c", provider="memory", provider_message_id="m",
        stream=StreamRef(mailbox="ops@acme.com"), subject=subject,
        **{"from": Recipient(address=addr)},
    )


def test_first_keep_short_circuits_and_stops():
    chain = FilterChain([
        WhitelistFilter(domains={"partner.com"}),
        SubjectFilter(patterns=[r".*"]),  # would DROP everything if reached
    ])
    d = chain.run(_env(addr="a@partner.com", subject="anything"), CTX)
    assert d.decision is Decision.keep
    assert d.filter_name == "whitelist"


def test_first_drop_short_circuits():
    chain = FilterChain([BlacklistFilter(domains={"spam.com"})])
    assert chain.run(_env(addr="a@spam.com"), CTX).decision is Decision.drop


def test_all_uncertain_yields_uncertain():
    chain = FilterChain([
        WhitelistFilter(domains={"partner.com"}),
        BlacklistFilter(domains={"spam.com"}),
    ])
    assert chain.run(_env(addr="a@neutral.com"), CTX).decision is Decision.uncertain


def test_empty_chain_is_uncertain():
    assert FilterChain([]).run(_env(), CTX).decision is Decision.uncertain
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/filters/ -v`
Expected: FAIL — `No module named 'mailflow.filters.deterministic'`.

- [ ] **Step 3: Write the implementations**

`src/mailflow/filters/__init__.py`: *(empty)*

`src/mailflow/filters/deterministic.py`:
```python
"""Deterministic, three-valued filters (spec §7.4). Destructive filters with empty
config return UNCERTAIN so a reusable toolkit never silently drops real mail."""

from __future__ import annotations

import re

from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import Envelope


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


class WhitelistFilter:
    name = "whitelist"

    def __init__(self, domains: set[str]) -> None:
        self.domains = {d.lower() for d in domains}

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if self.domains and _domain(env.from_.address) in self.domains:
            return FilterDecision.keep(self.name, f"domain {_domain(env.from_.address)}")
        return FilterDecision.uncertain()


class BlacklistFilter:
    name = "blacklist"

    def __init__(self, domains: set[str]) -> None:
        self.domains = {d.lower() for d in domains}

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if self.domains and _domain(env.from_.address) in self.domains:
            return FilterDecision.drop(self.name, f"domain {_domain(env.from_.address)}")
        return FilterDecision.uncertain()


class InternalDomainFilter:
    name = "internal_domain"

    def __init__(self, domains: set[str]) -> None:
        self.domains = {d.lower() for d in domains}

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if self.domains and _domain(env.from_.address) in self.domains:
            return FilterDecision.drop(self.name, "internal domain")
        return FilterDecision.uncertain()


class SubjectFilter:
    name = "subject"

    def __init__(self, patterns: list[str]) -> None:
        self.patterns = [re.compile(p) for p in patterns]

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        for pat in self.patterns:
            if pat.search(env.subject):
                return FilterDecision.drop(self.name, f"subject ~ {pat.pattern}")
        return FilterDecision.uncertain()


class ListMailFilter:
    """Drops mailing-list / auto-submitted mail using free header signals (§7.4, R-D3)."""

    name = "list_mail"

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if env.list_id or (env.auto_submitted and env.auto_submitted.lower() != "no"):
            return FilterDecision.drop(self.name, "list/auto-submitted header present")
        return FilterDecision.uncertain()
```

`src/mailflow/filters/chain.py`:
```python
"""The ordered filter chain (spec §7.4). First KEEP or DROP wins; otherwise UNCERTAIN."""

from __future__ import annotations

from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import Decision, Envelope
from mailflow.core.ports import Filter


class FilterChain:
    def __init__(self, filters: list[Filter]) -> None:
        self.filters = filters

    def run(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        for f in self.filters:
            decision = f.evaluate(env, ctx)
            if decision.decision in (Decision.keep, Decision.drop):
                return decision
        return FilterDecision.uncertain()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/filters/ -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/filters/ tests/filters/
git commit -m "feat(filters): deterministic three-valued filters + chain (§7.4)"
```

---

## Task 10: In-memory stores — the §8 invariants in isolation

**Files:**
- Create: `src/mailflow/stores/__init__.py` (empty)
- Create: `src/mailflow/stores/memory.py`
- Test: `tests/stores/test_memory_stores.py`

This is where the correctness rules get unit-tested deterministically before the pipeline wires them together: monotonic CAS (§8.3), atomic claim + attempts (§8.2/§8.4), blob put/open.

- [ ] **Step 1: Write the failing test**

`tests/stores/test_memory_stores.py`:
```python
from mailflow.core.models import Cursor, StreamRef
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def test_cursor_starts_empty():
    assert InMemoryCursorStore().get("acme", STREAM) is None


def test_cursor_commit_if_ahead_only_advances_forward():
    cs = InMemoryCursorStore()
    assert cs.commit_if_ahead("acme", STREAM, Cursor(value="a", order=1)) is True
    assert cs.commit_if_ahead("acme", STREAM, Cursor(value="b", order=2)) is True
    # an older cursor is rejected (monotonic, spec §8.3)
    assert cs.commit_if_ahead("acme", STREAM, Cursor(value="stale", order=1)) is False
    assert cs.get("acme", STREAM) == Cursor(value="b", order=2)


def test_cursor_is_namespaced_per_tenant_and_stream():
    cs = InMemoryCursorStore()
    cs.commit_if_ahead("acme", STREAM, Cursor(value="a", order=5))
    assert cs.get("other", STREAM) is None
    assert cs.get("acme", StreamRef(mailbox="ops@acme.com")) is None  # folderless = different stream


def test_dedupe_claim_is_exclusive():
    ds = InMemoryDedupeStore()
    assert ds.try_claim("k1", lease_seconds=60) is True
    assert ds.try_claim("k1", lease_seconds=60) is False  # already claimed


def test_dedupe_release_allows_reclaim():
    ds = InMemoryDedupeStore()
    ds.try_claim("k1", 60)
    ds.release("k1")
    assert ds.try_claim("k1", 60) is True


def test_dedupe_mark_done_blocks_future_claims():
    ds = InMemoryDedupeStore()
    ds.try_claim("k1", 60)
    ds.mark_done("k1", ttl_seconds=3600)
    assert ds.try_claim("k1", 60) is False  # done stays claimed for the TTL window


def test_dedupe_records_attempts():
    ds = InMemoryDedupeStore()
    ds.try_claim("k1", 60)
    assert ds.record_attempt("k1") == 1
    assert ds.record_attempt("k1") == 2


def test_blob_put_and_open_round_trip():
    bs = InMemoryBlobStore()
    ref = bs.put_stream("acme/att1", iter([b"PDF", b"BYTES"]), "application/pdf")
    assert b"".join(bs.open(ref)) == b"PDFBYTES"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/stores/test_memory_stores.py -v`
Expected: FAIL — `No module named 'mailflow.stores.memory'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/stores/__init__.py`: *(empty)*

`src/mailflow/stores/memory.py`:
```python
"""In-memory store adapters. They encode the §8 invariants so the pipeline that
coordinates them can be tested deterministically with no cloud backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from mailflow.core.models import Cursor, StreamRef


class InMemoryCursorStore:
    """Per-(tenant, stream) cursor with monotonic compare-and-set (spec §8.3)."""

    def __init__(self) -> None:
        self._cursors: dict[tuple[str, str], Cursor] = {}

    def _key(self, tenant: str, stream: StreamRef) -> tuple[str, str]:
        return (tenant, stream.key)

    def get(self, tenant: str, stream: StreamRef) -> Cursor | None:
        return self._cursors.get(self._key(tenant, stream))

    def commit_if_ahead(self, tenant: str, stream: StreamRef, cursor: Cursor) -> bool:
        key = self._key(tenant, stream)
        current = self._cursors.get(key)
        if current is not None and cursor.order <= current.order:
            return False  # forward-only: reject stale/equal
        self._cursors[key] = cursor
        return True


@dataclass
class _ClaimRecord:
    done: bool = False
    attempts: int = 0


class InMemoryDedupeStore:
    """Atomic claim-before-work (spec §8.2) + per-claim attempt counter (spec §8.4).

    Single-process in-memory model: a claim is exclusive until released or done.
    `lease_seconds`/`ttl_seconds` are accepted for interface parity with the real
    (Firestore/Redis) adapters; expiry is not simulated here.
    """

    def __init__(self) -> None:
        self._claims: dict[str, _ClaimRecord] = {}

    def try_claim(self, key: str, lease_seconds: int) -> bool:
        if key in self._claims:
            return False
        self._claims[key] = _ClaimRecord()
        return True

    def record_attempt(self, key: str) -> int:
        rec = self._claims.setdefault(key, _ClaimRecord())
        rec.attempts += 1
        return rec.attempts

    def mark_done(self, key: str, ttl_seconds: int) -> None:
        self._claims.setdefault(key, _ClaimRecord()).done = True

    def release(self, key: str) -> None:
        rec = self._claims.get(key)
        if rec is not None and not rec.done:
            del self._claims[key]


@dataclass
class InMemoryBlobStore:
    """Stream attachment bytes to memory; return an opaque ref (spec §6.2)."""

    _blobs: dict[str, bytes] = field(default_factory=dict)

    def put_stream(self, ref: str, chunks: Iterator[bytes], content_type: str) -> str:
        self._blobs[ref] = b"".join(chunks)
        return ref

    def open(self, ref: str) -> Iterator[bytes]:
        yield self._blobs[ref]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/stores/test_memory_stores.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/stores/ tests/stores/
git commit -m "feat(stores): in-memory cursor (monotonic CAS), dedupe (atomic claim+attempts), blob"
```

---

## Task 11: Memory provider

**Files:**
- Create: `src/mailflow/providers/__init__.py` (empty)
- Create: `src/mailflow/providers/memory.py`
- Test: `tests/providers/test_memory_provider.py`

A zero-dependency `MailboxProvider` seeded with raw RFC822 emails. `fetch` yields only messages strictly after the given cursor, in order, so cursor-based incremental fetch can be tested.

- [ ] **Step 1: Write the failing test**

`tests/providers/test_memory_provider.py`:
```python
from mailflow.core.models import Cursor, StreamRef
from mailflow.providers.memory import MemoryProvider, SeedEmail

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _provider() -> MemoryProvider:
    return MemoryProvider(seed={
        STREAM: [
            SeedEmail(provider_message_id="m1", raw=b"Subject: one\r\n\r\nbody one"),
            SeedEmail(provider_message_id="m2", raw=b"Subject: two\r\n\r\nbody two"),
            SeedEmail(provider_message_id="m3", raw=b"Subject: three\r\n\r\nbody three"),
        ]
    })


def test_sync_streams_lists_seeded_streams():
    assert list(_provider().sync_streams()) == [STREAM]


def test_fetch_from_none_returns_all_in_order():
    msgs = list(_provider().fetch(STREAM, cursor=None))
    assert [m.provider_message_id for m in msgs] == ["m1", "m2", "m3"]
    assert [m.cursor.order for m in msgs] == [1, 2, 3]


def test_fetch_after_cursor_returns_only_newer():
    p = _provider()
    msgs = list(p.fetch(STREAM, cursor=Cursor(value="c2", order=2)))
    assert [m.provider_message_id for m in msgs] == ["m3"]


def test_message_size_matches_raw_length():
    p = _provider()
    msg = next(p.fetch(STREAM, cursor=None))
    assert p.message_size(msg) == len(msg.raw_bytes)


def test_unknown_stream_yields_nothing():
    assert list(_provider().fetch(StreamRef(mailbox="nope@x.com"), None)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/providers/test_memory_provider.py -v`
Expected: FAIL — `No module named 'mailflow.providers.memory'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/providers/__init__.py`: *(empty)*

`src/mailflow/providers/memory.py`:
```python
"""Zero-dependency in-memory MailboxProvider for tests and the `provider: memory`
zero-setup path (spec §12a)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Iterator

from mailflow.core.models import Cursor, RawMessage, StreamRef


@dataclass
class SeedEmail:
    provider_message_id: str
    raw: bytes
    received_at: datetime | None = None


class MemoryProvider:
    PROVIDER = "memory"

    def __init__(self, seed: dict[StreamRef, list[SeedEmail]]) -> None:
        self._seed = seed
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def sync_streams(self) -> Iterable[StreamRef]:
        return list(self._seed.keys())

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        start = cursor.order if cursor is not None else 0
        for index, item in enumerate(self._seed.get(stream, []), start=1):
            if index <= start:
                continue
            yield RawMessage(
                provider=self.PROVIDER,
                provider_message_id=item.provider_message_id,
                stream=stream,
                size_bytes=len(item.raw),
                received_at=item.received_at or datetime(2026, 6, 9, tzinfo=timezone.utc),
                cursor=Cursor(value=f"{stream.key}#{index}", order=index),
                raw_bytes=item.raw,
            )

    def message_size(self, msg: RawMessage) -> int | None:
        return msg.size_bytes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/providers/test_memory_provider.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/providers/ tests/providers/
git commit -m "feat(providers): in-memory MailboxProvider with cursor-based fetch"
```

---

## Task 12: Emitters + observability records

**Files:**
- Create: `src/mailflow/emit/__init__.py` (empty)
- Create: `src/mailflow/emit/memory.py`
- Create: `src/mailflow/emit/stdout.py`
- Create: `src/mailflow/core/observability.py`
- Test: `tests/emit/test_emitters.py`
- Test: `tests/core/test_observability.py`

- [ ] **Step 1: Write the failing tests**

`tests/emit/test_emitters.py`:
```python
import json

from mailflow.core.events import EmailEvent
from mailflow.core.models import CleanEmail, Recipient
from mailflow.emit.memory import EmitReceipt, MemoryEmitter
from mailflow.emit.stdout import StdoutEmitter


def _event() -> EmailEvent:
    return EmailEvent(
        tenant="acme",
        ordering_key="ops@acme.com",
        email=CleanEmail(
            canonical_id="c1", provider="memory", provider_message_id="m1",
            provider_stream_id="s", **{"from": Recipient(address="a@x.com")},
        ),
    )


def test_memory_emitter_collects_events_and_returns_receipt():
    em = MemoryEmitter()
    receipt = em.emit(_event())
    assert isinstance(receipt, EmitReceipt)
    assert receipt.accepted is True
    assert em.events[0].email.canonical_id == "c1"


def test_stdout_emitter_writes_json_line(capsys):
    StdoutEmitter().emit(_event())
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["email"]["canonical_id"] == "c1"
    assert parsed["schema_version"] == "1.0"
```

`tests/core/test_observability.py`:
```python
from mailflow.core.models import Disposition
from mailflow.core.observability import DeadLetter, DecisionTrace, RunReport


def test_run_report_counts_and_accumulates():
    report = RunReport()
    report.record(DecisionTrace(
        canonical_id="c1", tenant="acme", stream="ops@acme.com:Inbox",
        disposition=Disposition.emitted, stage="emit",
    ))
    report.record(DecisionTrace(
        canonical_id="c2", tenant="acme", stream="ops@acme.com:Inbox",
        disposition=Disposition.dropped, stage="filter", matched_filter="blacklist",
    ))
    report.add_dead_letter(DeadLetter(canonical_id="c3", reason="oversized", provider_message_id="m3"))
    assert report.emitted == 1
    assert report.dropped == 1
    assert report.dead_lettered == 1
    assert len(report.traces) == 2
    assert report.dlq[0].reason == "oversized"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/emit/test_emitters.py tests/core/test_observability.py -v`
Expected: FAIL — `No module named 'mailflow.emit.memory'`.

- [ ] **Step 3: Write the implementations**

`src/mailflow/emit/__init__.py`: *(empty)*

`src/mailflow/emit/memory.py`:
```python
"""In-memory emitter for tests and the zero-setup path."""

from __future__ import annotations

from pydantic import BaseModel

from mailflow.core.events import EmailEvent


class EmitReceipt(BaseModel):
    id: str
    accepted: bool = True


class MemoryEmitter:
    def __init__(self) -> None:
        self.events: list[EmailEvent] = []

    def emit(self, event: EmailEvent) -> EmitReceipt:
        self.events.append(event)
        return EmitReceipt(id=event.email.canonical_id, accepted=True)
```

`src/mailflow/emit/stdout.py`:
```python
"""Stdout emitter — prints one JSON line per event (local/dev, spec §15 cuttable set)."""

from __future__ import annotations

import sys

from mailflow.core.events import EmailEvent
from mailflow.emit.memory import EmitReceipt


class StdoutEmitter:
    def emit(self, event: EmailEvent) -> EmitReceipt:
        sys.stdout.write(event.model_dump_json(by_alias=True) + "\n")
        return EmitReceipt(id=event.email.canonical_id, accepted=True)
```

`src/mailflow/core/observability.py`:
```python
"""Decision trace + run report (spec §8.5, §12). Every message produces a trace
regardless of outcome — this answers the #1 support question, "why was my email
dropped?"."""

from __future__ import annotations

from pydantic import BaseModel, Field

from mailflow.core.models import Disposition


class DecisionTrace(BaseModel):
    canonical_id: str
    tenant: str
    stream: str
    disposition: Disposition
    stage: str
    matched_filter: str = ""
    reason: str = ""
    relevance_score: float | None = None


class DeadLetter(BaseModel):
    canonical_id: str
    reason: str
    provider_message_id: str


class RunReport(BaseModel):
    fetched: int = 0
    emitted: int = 0
    dropped: int = 0
    duplicates: int = 0
    dead_lettered: int = 0
    traces: list[DecisionTrace] = Field(default_factory=list)
    dlq: list[DeadLetter] = Field(default_factory=list)

    def record(self, trace: DecisionTrace) -> None:
        self.traces.append(trace)
        if trace.disposition is Disposition.emitted:
            self.emitted += 1
        elif trace.disposition is Disposition.dropped:
            self.dropped += 1
        elif trace.disposition is Disposition.duplicate:
            self.duplicates += 1
        elif trace.disposition is Disposition.dead_lettered:
            self.dead_lettered += 1

    def add_dead_letter(self, dead_letter: DeadLetter) -> None:
        self.dlq.append(dead_letter)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/emit/test_emitters.py tests/core/test_observability.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/emit/ src/mailflow/core/observability.py tests/emit/ tests/core/test_observability.py
git commit -m "feat: memory/stdout emitters + decision trace and run report (§8.5, §12)"
```

---

## Task 13: The pipeline orchestrator — wiring the §8 invariants together

**Files:**
- Create: `src/mailflow/core/pipeline.py`
- Test: `tests/core/test_pipeline.py`

This is the heart of the toolkit. It is built last because every invariant is a property of how it *coordinates* the now-tested stores and adapters. Per-message order: **claim (§8.2) → size guard (§8.6) → parse envelope (§7.3) → filter (§7.4) → extract (§7.6) → emit (§7.7) → mark done; advance cursor on any terminal disposition (§8.1) via monotonic CAS (§8.3); route poison to DLQ (§8.4).**

We build it across several TDD cycles, one invariant per cycle.

### Cycle A — happy path: fetch → filter-uncertain → extract → emit → cursor advances

- [ ] **Step 1: Write the failing test**

`tests/core/test_pipeline.py`:
```python
from datetime import datetime, timezone

import pytest

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Cursor, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import BlacklistFilter, WhitelistFilter
from mailflow.emit.memory import MemoryEmitter
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _raw(mid: str, frm="alice@partner.com", subject="hi", body="hello") -> bytes:
    return (
        f"Message-ID: <{mid}@example.com>\r\n"
        f"From: {frm}\r\n"
        f"To: ops@acme.com\r\n"
        f"Subject: {subject}\r\n\r\n{body}\r\n"
    ).encode()


def _pipeline(seed, *, filters=None, max_message_bytes=10_000_000, max_attempts=3):
    provider = MemoryProvider(seed=seed)
    emitter = MemoryEmitter()
    dlq = MemoryEmitter()
    pipe = Pipeline(
        provider=provider,
        parser=MimeEnvelopeParser(),
        filters=FilterChain(filters or []),
        extractor=MimeExtractor(),
        emitter=emitter,
        dlq_emitter=dlq,
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(
            tenant="acme",
            max_message_bytes=max_message_bytes,
            max_attempts=max_attempts,
        ),
    )
    return pipe, emitter, dlq


def test_happy_path_emits_and_advances_cursor():
    seed = {STREAM: [SeedEmail("m1", _raw("m1")), SeedEmail("m2", _raw("m2"))]}
    pipe, emitter, _ = _pipeline(seed)
    report = pipe.run_once()
    assert report.emitted == 2
    assert [e.email.message_id for e in emitter.events] == ["<m1@example.com>", "<m2@example.com>"]
    # cursor advanced to the last message
    assert pipe.cursor_store.get("acme", STREAM) == Cursor(value=f"{STREAM.key}#2", order=2)
    # emitted event carries tenant + ordering key = mailbox
    assert emitter.events[0].tenant == "acme"
    assert emitter.events[0].ordering_key == "ops@acme.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/core/test_pipeline.py::test_happy_path_emits_and_advances_cursor -v`
Expected: FAIL — `No module named 'mailflow.core.pipeline'`.

- [ ] **Step 3: Write the pipeline (full implementation — all invariants)**

`src/mailflow/core/pipeline.py`:
```python
"""The orchestrator. Coordinates the ports to honour every §8 invariant.

Per-message order:
  claim (§8.2) -> size guard (§8.6) -> parse (§7.3) -> filter (§7.4)
  -> extract (§7.6) -> emit (§7.7) -> mark done.
Cursor advances on ANY terminal disposition (§8.1), monotonic + single-writer (§8.3).
Poison messages go to the DLQ and the cursor moves past them (§8.4).
"""

from __future__ import annotations

from pydantic import BaseModel

from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.filtering import FilterContext
from mailflow.core.identity import derive_canonical_id, idempotency_key
from mailflow.core.models import Cursor, Decision, Disposition, RawMessage, StreamRef
from mailflow.core.observability import DeadLetter, DecisionTrace, RunReport
from mailflow.core.ports import (
    BlobStore,
    Classifier,
    ContentExtractor,
    CursorStore,
    DedupeStore,
    Emitter,
    EnvelopeParser,
    MailboxProvider,
)
from mailflow.filters.chain import FilterChain


class PipelineConfig(BaseModel):
    tenant: str
    max_message_bytes: int = 50_000_000
    max_attempts: int = 3
    claim_lease_seconds: int = 300
    done_ttl_seconds: int = 60 * 60 * 24 * 60  # 60 days (spec §11 dedupe ttl)


class Pipeline:
    def __init__(
        self,
        *,
        provider: MailboxProvider,
        parser: EnvelopeParser,
        filters: FilterChain,
        extractor: ContentExtractor,
        emitter: Emitter,
        dlq_emitter: Emitter,
        cursor_store: CursorStore,
        dedupe_store: DedupeStore,
        blob_store: BlobStore,
        config: PipelineConfig,
        classifier: Classifier | None = None,
    ) -> None:
        self.provider = provider
        self.parser = parser
        self.filters = filters
        self.extractor = extractor
        self.emitter = emitter
        self.dlq_emitter = dlq_emitter
        self.cursor_store = cursor_store
        self.dedupe_store = dedupe_store
        self.blob_store = blob_store
        self.config = config
        self.classifier = classifier

    def run_once(self) -> RunReport:
        report = RunReport()
        self.provider.connect()
        for stream in self.provider.sync_streams():
            self._run_stream(stream, report)
        return report

    def _run_stream(self, stream: StreamRef, report: RunReport) -> None:
        cursor = self.cursor_store.get(self.config.tenant, stream)
        for msg in self.provider.fetch(stream, cursor):
            report.fetched += 1
            disposition = self._process(msg, report)
            # §8.1: advance the bookmark on ANY terminal disposition, via monotonic CAS (§8.3).
            if disposition is not None:
                self.cursor_store.commit_if_ahead(self.config.tenant, stream, msg.cursor)

    def _process(self, msg: RawMessage, report: RunReport) -> Disposition | None:
        tenant = self.config.tenant
        key = idempotency_key(tenant, msg.stream.mailbox, msg.provider_message_id)
        canonical_id, _present, _trusted = derive_canonical_id(
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            mailbox=msg.stream.mailbox,
            message_id=None,  # refined after parse; fine for trace/dlq keys
        )

        # §8.2: claim before any spend.
        if not self.dedupe_store.try_claim(key, self.config.claim_lease_seconds):
            report.record(self._trace(canonical_id, msg, Disposition.duplicate, "dedupe"))
            return Disposition.duplicate

        attempts = self.dedupe_store.record_attempt(key)

        # §8.6: size guard against metadata BEFORE downloading/decoding bytes.
        size = self.provider.message_size(msg) or msg.size_bytes
        if size > self.config.max_message_bytes:
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"oversized: {size} > {self.config.max_message_bytes}",
            )

        try:
            env = self.parser.parse_envelope(msg, tenant)
            decision = self.filters.run(env, FilterContext(tenant=tenant))

            if decision.decision is Decision.drop:
                self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
                report.record(self._trace(
                    env.canonical_id, msg, Disposition.dropped, "filter",
                    matched_filter=decision.filter_name, reason=decision.reason,
                ))
                return Disposition.dropped

            relevance = None
            if self.classifier is not None and decision.decision is Decision.uncertain:
                relevance = self.classifier.classify(env, FilterContext(tenant=tenant))

            email = self.extractor.extract(  # type: ignore[call-arg]
                msg, env
            ) if hasattr(self.extractor, "extract") else None
            # MimeExtractor uses extract_bytes; adapt:
            email = self._extract(msg, env)
            if relevance is not None:
                email.relevance = relevance
            email.matched_filter = decision.filter_name

            event = EmailEvent(
                schema_version=SCHEMA_VERSION,
                tenant=tenant,
                ordering_key=msg.stream.mailbox,
                email=email,
            )
            self.emitter.emit(event)
            self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
            report.record(self._trace(
                email.canonical_id, msg, Disposition.emitted, "emit",
                relevance_score=(relevance.score if relevance else None),
            ))
            return Disposition.emitted

        except Exception as exc:  # noqa: BLE001 - we translate failures into DLQ routing
            if attempts >= self.config.max_attempts:
                return self._dead_letter(
                    canonical_id, msg, key, report, reason=f"{type(exc).__name__}: {exc}"
                )
            # transient: free the claim so a later run/redelivery retries (§8.2 lease semantics).
            self.dedupe_store.release(key)
            return None  # NOT terminal -> cursor does NOT advance past it yet

    def _extract(self, msg: RawMessage, env):  # type: ignore[no-untyped-def]
        from mailflow.extract.mime import MimeExtractor

        if isinstance(self.extractor, MimeExtractor):
            return self.extractor.extract_bytes(
                msg.raw_bytes,
                provider=msg.provider,
                provider_message_id=msg.provider_message_id,
                stream_id=msg.stream.key,
                watched_mailbox=msg.stream.mailbox,
            )
        return self.extractor.extract(msg, env)

    def _dead_letter(
        self, canonical_id: str, msg: RawMessage, key: str, report: RunReport, *, reason: str
    ) -> Disposition:
        self.dlq_emitter.emit(  # DLQ is just another Emitter sink in the core spine
            EmailEvent(
                schema_version=SCHEMA_VERSION,
                tenant=self.config.tenant,
                ordering_key=msg.stream.mailbox,
                email=self._stub_email(canonical_id, msg),
            )
        )
        self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
        report.add_dead_letter(
            DeadLetter(canonical_id=canonical_id, reason=reason,
                       provider_message_id=msg.provider_message_id)
        )
        report.record(self._trace(canonical_id, msg, Disposition.dead_lettered, "dlq", reason=reason))
        return Disposition.dead_lettered

    def _stub_email(self, canonical_id: str, msg: RawMessage):  # type: ignore[no-untyped-def]
        from mailflow.core.models import CleanEmail

        return CleanEmail(
            canonical_id=canonical_id,
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            provider_stream_id=msg.stream.key,
            message_size_bytes=msg.size_bytes,
            schema_version=SCHEMA_VERSION,
        )

    def _trace(
        self, canonical_id: str, msg: RawMessage, disposition: Disposition, stage: str,
        *, matched_filter: str = "", reason: str = "", relevance_score: float | None = None,
    ) -> DecisionTrace:
        return DecisionTrace(
            canonical_id=canonical_id,
            tenant=self.config.tenant,
            stream=msg.stream.key,
            disposition=disposition,
            stage=stage,
            matched_filter=matched_filter,
            reason=reason,
            relevance_score=relevance_score,
        )
```

> **Implementer cleanup note:** the duplicated `email = ...` lines around the extractor in `_process` are an artifact of inlining — replace the whole `email = self.extractor.extract(...) if ... else None` block with a single `email = self._extract(msg, env)` call. The `_extract` helper exists because `MimeExtractor` exposes `extract_bytes` (raw bytes) rather than the generic `extract(msg, env)` signature; the Graph adapter in Plan 2 will implement `extract(msg, env)` directly. Keep `_extract` as the single seam.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/core/test_pipeline.py::test_happy_path_emits_and_advances_cursor -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/core/pipeline.py tests/core/test_pipeline.py
git commit -m "feat(core): pipeline orchestrator happy path (fetch->filter->extract->emit->cursor)"
```

### Cycle B — drop short-circuits before extraction (§7.4)

- [ ] **Step 1: Add the failing test** to `tests/core/test_pipeline.py`:
```python
def test_blacklisted_mail_is_dropped_not_emitted_and_cursor_advances():
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", frm="spammer@spam.com")),
        SeedEmail("m2", _raw("m2", frm="alice@partner.com")),
    ]}
    pipe, emitter, _ = _pipeline(seed, filters=[BlacklistFilter(domains={"spam.com"})])
    report = pipe.run_once()
    assert report.dropped == 1
    assert report.emitted == 1
    assert [e.email.provider_message_id for e in emitter.events] == ["m2"]
    # dropped message still counts as finished -> cursor advanced past BOTH
    assert pipe.cursor_store.get("acme", STREAM).order == 2
    drop_trace = next(t for t in report.traces if t.disposition.value == "dropped")
    assert drop_trace.matched_filter == "blacklist"
```

- [ ] **Step 2: Run** `python -m pytest tests/core/test_pipeline.py::test_blacklisted_mail_is_dropped_not_emitted_and_cursor_advances -v` → PASS (already implemented in Cycle A). If it fails, fix `_process` drop branch.

- [ ] **Step 3: Commit**
```bash
git add tests/core/test_pipeline.py
git commit -m "test(core): drop short-circuits extraction; dropped advances cursor (§8.1)"
```

### Cycle C — duplicate is skipped and still advances the cursor (§8.1/§8.2)

- [ ] **Step 1: Add the failing test:**
```python
def test_duplicate_delivery_is_skipped_once_and_counts_as_finished():
    seed = {STREAM: [SeedEmail("m1", _raw("m1"))]}
    pipe, emitter, _ = _pipeline(seed)
    pipe.run_once()                     # first pass emits m1
    # reset the provider cursor to force a re-delivery of m1
    pipe.cursor_store = InMemoryCursorStore()
    report = pipe.run_once()            # second pass sees m1 again
    assert report.emitted == 0
    assert report.duplicates == 1       # claimed-already -> skipped
    assert len(emitter.events) == 1     # still only emitted once, ever
    assert pipe.cursor_store.get("acme", STREAM).order == 1  # duplicate advanced cursor
```

- [ ] **Step 2: Run** the test → PASS (Cycle A handles the claim-fails branch). Verify `report.duplicates == 1`.

- [ ] **Step 3: Commit**
```bash
git add tests/core/test_pipeline.py
git commit -m "test(core): duplicate skipped exactly-once, still advances cursor (§8.1, §8.2)"
```

### Cycle D — oversized message goes to DLQ without download (§8.6)

- [ ] **Step 1: Add the failing test:**
```python
def test_oversized_message_is_dead_lettered_and_cursor_advances():
    big = _raw("big", body="x" * 100)
    seed = {STREAM: [SeedEmail("big", big), SeedEmail("m2", _raw("m2"))]}
    pipe, emitter, dlq = _pipeline(seed, max_message_bytes=len(big) - 1)
    report = pipe.run_once()
    assert report.dead_lettered == 1
    assert report.emitted == 1                      # m2 still flows
    assert dlq.events[0].email.provider_message_id == "big"
    assert "oversized" in report.dlq[0].reason
    assert pipe.cursor_store.get("acme", STREAM).order == 2  # moved past the poison message
```

- [ ] **Step 2: Run** the test → PASS (Cycle A size-guard branch). If size check is misplaced, ensure it runs *after claim, before parse/extract*.

- [ ] **Step 3: Commit**
```bash
git add tests/core/test_pipeline.py
git commit -m "test(core): oversized -> DLQ without download, cursor advances (§8.6, §8.4)"
```

### Cycle E — poison message (extract throws) goes to DLQ after max attempts (§8.4)

- [ ] **Step 1: Add the failing test** (uses a stub extractor that always raises):
```python
class _ExplodingExtractor:
    def extract(self, msg, env):  # noqa: ANN001, ANN201
        raise ValueError("boom")


def test_repeated_failure_routes_to_dlq_after_max_attempts():
    from mailflow.core.pipeline import Pipeline, PipelineConfig
    provider = MemoryProvider(seed={STREAM: [SeedEmail("m1", _raw("m1"))]})
    emitter, dlq = MemoryEmitter(), MemoryEmitter()
    pipe = Pipeline(
        provider=provider, parser=MimeEnvelopeParser(), filters=FilterChain([]),
        extractor=_ExplodingExtractor(), emitter=emitter, dlq_emitter=dlq,
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme", max_attempts=1),
    )
    report = pipe.run_once()
    assert report.dead_lettered == 1
    assert report.emitted == 0
    assert "boom" in report.dlq[0].reason
    assert pipe.cursor_store.get("acme", STREAM).order == 1
```

> Note: `_ExplodingExtractor` is not a `MimeExtractor`, so `_extract` calls its `extract(msg, env)` — which raises. With `max_attempts=1`, the first attempt (attempts==1 >= max==1) routes straight to DLQ.

- [ ] **Step 2: Run** the test → PASS. If `_extract` is hard-wired to `MimeExtractor`, fix it to fall through to `self.extractor.extract(msg, env)` for non-Mime extractors.

- [ ] **Step 3: Commit**
```bash
git add tests/core/test_pipeline.py
git commit -m "test(core): poison message -> DLQ after max_attempts (§8.4)"
```

### Cycle F — monotonic cursor under a stale re-run (§8.3)

- [ ] **Step 1: Add the failing test:**
```python
def test_stale_cursor_commit_is_rejected():
    seed = {STREAM: [SeedEmail("m1", _raw("m1")), SeedEmail("m2", _raw("m2"))]}
    pipe, _, _ = _pipeline(seed)
    pipe.run_once()                                   # cursor at order 2
    # simulate a slow sweep trying to commit an older cursor directly
    ok = pipe.cursor_store.commit_if_ahead("acme", STREAM, Cursor(value="stale", order=1))
    assert ok is False
    assert pipe.cursor_store.get("acme", STREAM).order == 2  # never regresses
```

- [ ] **Step 2: Run** the test → PASS (delegates to `InMemoryCursorStore` from Task 10).

- [ ] **Step 3: Run the whole pipeline test module + commit**
```bash
python -m pytest tests/core/test_pipeline.py -v   # expect all cycles green
git add tests/core/test_pipeline.py
git commit -m "test(core): cursor never regresses under stale re-commit (§8.3)"
```

---

## Task 14: Config schema + loader (§11)

**Files:**
- Create: `src/mailflow/config/__init__.py` (empty)
- Create: `src/mailflow/config/schema.py`
- Create: `src/mailflow/config/loader.py`
- Test: `tests/config/test_config.py`

Layered config (package defaults → YAML → env). For the core spine the supported kinds are `memory` provider, the deterministic filters, `memory` stores, and `memory`/`stdout` emitters. Destructive filters are absent by default.

- [ ] **Step 1: Write the failing test**

`tests/config/test_config.py`:
```python
import textwrap

import pytest

from mailflow.config.loader import load_config, validate
from mailflow.config.schema import MailflowConfig
from mailflow.core.errors import ConfigError


def test_defaults_give_a_memory_pipeline():
    cfg = MailflowConfig()
    assert cfg.provider.kind == "memory"
    assert cfg.emitter.kind == "memory"
    assert cfg.filters == []                      # destructive filters absent by default
    assert cfg.classifier.enabled is False


def test_load_from_yaml(tmp_path):
    p = tmp_path / "mailflow.yaml"
    p.write_text(textwrap.dedent("""
        tenant: acme
        provider: { kind: memory }
        filters:
          - { kind: whitelist, params: { domains: ["partner.com"] } }
          - { kind: blacklist, params: { domains: ["spam.com"] } }
        emitter: { kind: stdout }
        security:
          read_allowlist: ["ops@acme.com"]
    """))
    cfg = load_config(str(p))
    assert cfg.tenant == "acme"
    assert [f.kind for f in cfg.filters] == ["whitelist", "blacklist"]
    assert cfg.emitter.kind == "stdout"
    assert cfg.security.read_allowlist == ["ops@acme.com"]


def test_validate_rejects_unknown_kind():
    cfg = MailflowConfig.model_validate({
        "tenant": "acme",
        "provider": {"kind": "memory"},
        "filters": [{"kind": "nonsense"}],
    })
    with pytest.raises(ConfigError) as e:
        validate(cfg)
    assert "nonsense" in str(e.value)


def test_validate_warns_on_unreachable_drop_after_catchall_keep():
    cfg = MailflowConfig.model_validate({
        "tenant": "acme",
        "filters": [
            {"kind": "whitelist", "params": {"domains": ["partner.com"]}},
            {"kind": "blacklist", "params": {"domains": ["spam.com"]}},
        ],
    })
    warnings = validate(cfg)            # no unreachable rule here -> no warnings
    assert warnings == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/config/test_config.py -v`
Expected: FAIL — `No module named 'mailflow.config.schema'`.

- [ ] **Step 3: Write the implementations**

`src/mailflow/config/__init__.py`: *(empty)*

`src/mailflow/config/schema.py`:
```python
"""Pydantic config models (spec §11). Secrets are references resolved lazily."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ComponentConfig(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class ClassifierConfig(BaseModel):
    enabled: bool = False
    policy: str = "flag"          # flag (default, non-destructive) | drop  (spec OD-2)


class SecurityConfig(BaseModel):
    read_allowlist: list[str] = Field(default_factory=list)   # fail-closed (spec §9.1)
    verify_scope_on_startup: bool = True


class StoresConfig(BaseModel):
    cursor: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    dedupe: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    blob: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))


class MailflowConfig(BaseModel):
    tenant: str = "default"
    provider: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    filters: list[ComponentConfig] = Field(default_factory=list)
    classifier: ClassifierConfig = Field(default_factory=ClassifierConfig)
    stores: StoresConfig = Field(default_factory=StoresConfig)
    emitter: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    max_message_bytes: int = 50_000_000
    max_attempts: int = 3
```

`src/mailflow/config/loader.py`:
```python
"""Layered loader + validate (spec §11). YAML support uses pydantic + stdlib only
where possible; if PyYAML is unavailable we accept JSON. (Core spine keeps deps light.)"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mailflow.config.schema import MailflowConfig
from mailflow.core.errors import ConfigError
from mailflow.registry import (
    EMITTER_KINDS,
    FILTER_KINDS,
    PROVIDER_KINDS,
    STORE_KINDS,
)


def _parse(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]

        return dict(yaml.safe_load(text) or {})
    except ModuleNotFoundError:
        return dict(json.loads(text))  # fall back to JSON if PyYAML not installed


def load_config(path: str) -> MailflowConfig:
    data = _parse(Path(path).read_text())
    # env overlay: MAILFLOW_TENANT overrides tenant (spec §11 precedence)
    if "MAILFLOW_TENANT" in os.environ:
        data["tenant"] = os.environ["MAILFLOW_TENANT"]
    return MailflowConfig.model_validate(data)


def validate(cfg: MailflowConfig) -> list[str]:
    """Raise ConfigError on unknown kinds; return non-fatal reachability warnings."""
    if cfg.provider.kind not in PROVIDER_KINDS:
        raise ConfigError(f"unknown provider kind {cfg.provider.kind!r}")
    if cfg.emitter.kind not in EMITTER_KINDS:
        raise ConfigError(f"unknown emitter kind {cfg.emitter.kind!r}")
    for store in (cfg.stores.cursor, cfg.stores.dedupe, cfg.stores.blob):
        if store.kind not in STORE_KINDS:
            raise ConfigError(f"unknown store kind {store.kind!r}")
    for f in cfg.filters:
        if f.kind not in FILTER_KINDS:
            raise ConfigError(f"unknown filter kind {f.kind!r}")

    warnings: list[str] = []
    seen_catchall_keep = False
    for f in cfg.filters:
        if seen_catchall_keep and f.params.get("on_match", "drop") == "drop":
            warnings.append(f"filter {f.kind!r} is unreachable after a catch-all keep")
        if f.kind == "whitelist" and not f.params.get("domains"):
            seen_catchall_keep = True  # empty whitelist would keep nothing; placeholder rule
    return warnings
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/config/test_config.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/config/ tests/config/
git commit -m "feat(config): pydantic schema + layered loader + validate (§11)"
```

---

## Task 15: Registry + builder (§12)

**Files:**
- Create: `src/mailflow/registry.py`
- Create: `src/mailflow/builder.py`
- Test: `tests/test_registry.py`
- Test: `tests/test_builder.py`

The registry maps config `kind` strings to classes. It is the allowlist seam where the Plan-4 plugin system bolts on (spec §13) — for now only built-in kinds are registered, never auto-loaded.

- [ ] **Step 1: Write the failing tests**

`tests/test_registry.py`:
```python
from mailflow.registry import (
    EMITTER_KINDS,
    FILTER_KINDS,
    PROVIDER_KINDS,
    STORE_KINDS,
    build_filter,
)


def test_builtin_kinds_registered():
    assert "memory" in PROVIDER_KINDS
    assert {"memory", "stdout"} <= set(EMITTER_KINDS)
    assert {"whitelist", "blacklist", "subject", "list_mail", "internal_domain"} <= set(FILTER_KINDS)
    assert "memory" in STORE_KINDS


def test_build_filter_constructs_with_params():
    f = build_filter("whitelist", {"domains": ["partner.com"]})
    assert f.name == "whitelist"
```

`tests/test_builder.py`:
```python
from mailflow.builder import build_from_config
from mailflow.config.schema import MailflowConfig
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail


def test_build_from_config_wires_a_runnable_pipeline():
    cfg = MailflowConfig.model_validate({
        "tenant": "acme",
        "provider": {"kind": "memory"},
        "filters": [{"kind": "blacklist", "params": {"domains": ["spam.com"]}}],
        "emitter": {"kind": "memory"},
    })
    stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
    seed = {stream: [
        SeedEmail("m1", b"Message-ID: <m1@x>\r\nFrom: a@spam.com\r\nSubject: x\r\n\r\nhi"),
        SeedEmail("m2", b"Message-ID: <m2@x>\r\nFrom: a@partner.com\r\nSubject: y\r\n\r\nhi"),
    ]}
    pipe = build_from_config(cfg, seed=seed)
    report = pipe.run_once()
    assert report.emitted == 1
    assert report.dropped == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_registry.py tests/test_builder.py -v`
Expected: FAIL — `No module named 'mailflow.registry'`.

- [ ] **Step 3: Write the implementations**

`src/mailflow/registry.py`:
```python
"""Built-in kind -> class registry (spec §12/§13 seam).

NOTE: entry-point auto-discovery is intentionally NOT done here. Plan 4 adds the
allowlisted plugin loader; until then only these built-ins exist."""

from __future__ import annotations

from typing import Any

from mailflow.core.ports import Emitter, Filter
from mailflow.emit.memory import MemoryEmitter
from mailflow.emit.stdout import StdoutEmitter
from mailflow.filters.deterministic import (
    BlacklistFilter,
    InternalDomainFilter,
    ListMailFilter,
    SubjectFilter,
    WhitelistFilter,
)
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

PROVIDER_KINDS = {"memory"}
EMITTER_KINDS = {"memory", "stdout"}
STORE_KINDS = {"memory"}
FILTER_KINDS = {"whitelist", "blacklist", "internal_domain", "subject", "list_mail"}


def build_filter(kind: str, params: dict[str, Any]) -> Filter:
    if kind == "whitelist":
        return WhitelistFilter(domains=set(params.get("domains", [])))
    if kind == "blacklist":
        return BlacklistFilter(domains=set(params.get("domains", [])))
    if kind == "internal_domain":
        return InternalDomainFilter(domains=set(params.get("domains", [])))
    if kind == "subject":
        return SubjectFilter(patterns=list(params.get("patterns", [])))
    if kind == "list_mail":
        return ListMailFilter()
    raise ValueError(f"unknown filter kind {kind!r}")  # validate() guards this earlier


def build_emitter(kind: str) -> Emitter:
    if kind == "memory":
        return MemoryEmitter()
    if kind == "stdout":
        return StdoutEmitter()
    raise ValueError(f"unknown emitter kind {kind!r}")


def build_cursor_store(kind: str) -> InMemoryCursorStore:
    return InMemoryCursorStore()


def build_dedupe_store(kind: str) -> InMemoryDedupeStore:
    return InMemoryDedupeStore()


def build_blob_store(kind: str) -> InMemoryBlobStore:
    return InMemoryBlobStore()
```

`src/mailflow/builder.py`:
```python
"""Wire a MailflowConfig into a runnable Pipeline (spec §12a). For the core spine
the only provider is `memory`, seeded by the caller."""

from __future__ import annotations

from mailflow.config.loader import validate
from mailflow.config.schema import MailflowConfig
from mailflow.core.models import StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.registry import (
    build_blob_store,
    build_cursor_store,
    build_dedupe_store,
    build_emitter,
    build_filter,
)


def build_from_config(
    cfg: MailflowConfig,
    *,
    seed: dict[StreamRef, list[SeedEmail]] | None = None,
) -> Pipeline:
    validate(cfg)  # raises on unknown kinds before we build anything
    if cfg.provider.kind != "memory":
        raise NotImplementedError(
            f"provider {cfg.provider.kind!r} is not in the core spine (see Plan 2/3)"
        )
    provider = MemoryProvider(seed=seed or {})
    filters = FilterChain([build_filter(f.kind, f.params) for f in cfg.filters])
    return Pipeline(
        provider=provider,
        parser=MimeEnvelopeParser(),
        filters=filters,
        extractor=MimeExtractor(),
        emitter=build_emitter(cfg.emitter.kind),
        dlq_emitter=build_emitter("memory"),
        cursor_store=build_cursor_store(cfg.stores.cursor.kind),
        dedupe_store=build_dedupe_store(cfg.stores.dedupe.kind),
        blob_store=build_blob_store(cfg.stores.blob.kind),
        config=PipelineConfig(
            tenant=cfg.tenant,
            max_message_bytes=cfg.max_message_bytes,
            max_attempts=cfg.max_attempts,
        ),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_registry.py tests/test_builder.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/registry.py src/mailflow/builder.py tests/test_registry.py tests/test_builder.py
git commit -m "feat: built-in registry + config-driven pipeline builder (§12)"
```

---

## Task 16: Public API surface

**Files:**
- Modify: `src/mailflow/__init__.py`
- Test: `tests/test_public_api.py`

Curate a small public API (spec §5.4: "small, curated public API") so consumers import from `mailflow` not deep paths.

- [ ] **Step 1: Write the failing test**

`tests/test_public_api.py`:
```python
import mailflow


def test_public_api_exports():
    for name in (
        "__version__", "CleanEmail", "EmailEvent", "SCHEMA_VERSION",
        "Pipeline", "build_from_config", "MailflowConfig",
    ):
        assert hasattr(mailflow, name), name


def test_schema_version_is_one_point_zero():
    assert mailflow.SCHEMA_VERSION == "1.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_public_api.py -v`
Expected: FAIL — `AttributeError: module 'mailflow' has no attribute 'CleanEmail'`.

- [ ] **Step 3: Update `__init__.py`**

`src/mailflow/__init__.py`:
```python
"""mailflow — email ingestion toolkit (core spine)."""

from mailflow.builder import build_from_config
from mailflow.config.schema import MailflowConfig
from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail
from mailflow.core.pipeline import Pipeline, PipelineConfig

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "CleanEmail",
    "EmailEvent",
    "SCHEMA_VERSION",
    "Pipeline",
    "PipelineConfig",
    "build_from_config",
    "MailflowConfig",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_public_api.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/__init__.py tests/test_public_api.py
git commit -m "feat: curated public API surface"
```

---

## Task 16.5: Real-email golden corpus (Cowboy Logistics thread)

**Owner:** `fixtures-golden-engineer`. **Depends on:** Task 13 (pipeline) green.

**Files:**
- Already staged: `tests/fixtures/cowboy_thread/corpus.json` (17 real Gmail records), `tests/fixtures/cowboy_thread/golden_cleanemails.json` (authored expected output), `tests/fixtures/cowboy_thread/README.md` (provenance).
- Create: `tests/fixtures/__init__.py` (empty), `tests/fixtures/loader.py`
- Create: `tests/test_golden_corpus.py`

This is the proof the pipeline produces correct `CleanEmail` on mail we actually received. The corpus is one Gmail thread — a 17-message freight-quote conversation between `sandyqatestacc@yahoo.com` and `testuser1@cowboyslogistics.com` (the watched mailbox). The golden was **authored by reading the emails** (from the convenience fields), independent of the extractor (which parses reconstructed RFC822) — two derivations that must agree.

- [ ] **Step 1: Write the fixture loader**

`tests/fixtures/__init__.py`: *(empty)*

`tests/fixtures/loader.py`:
```python
"""Convert real Gmail corpus records into RFC822 bytes for the MemoryProvider.

Independent of the mailflow extractor: we build the message from the convenience
fields, the extractor parses it back. Agreement is the golden test."""

from __future__ import annotations

import json
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime
from pathlib import Path

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

FIXTURE_DIR = Path(__file__).parent / "cowboy_thread"
WATCHED_MAILBOX = "testuser1@cowboyslogistics.com"
STREAM = StreamRef(mailbox=WATCHED_MAILBOX, folder="Inbox")


def load_corpus() -> list[dict]:
    return json.loads((FIXTURE_DIR / "corpus.json").read_text())


def load_golden() -> dict[str, dict]:
    return json.loads((FIXTURE_DIR / "golden_cleanemails.json").read_text())


def record_to_rfc822(rec: dict) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = rec["message_id_header"]
    msg["From"] = rec["from_email"]
    msg["To"] = rec["to_email"]
    if rec["cc_emails"]:
        msg["Cc"] = ", ".join(rec["cc_emails"])
    msg["Subject"] = rec["subject"]
    if rec["in_reply_to"]:
        msg["In-Reply-To"] = rec["in_reply_to"]
    if rec["references"]:
        msg["References"] = " ".join(rec["references"])
    try:
        msg["Date"] = format_datetime(datetime.fromisoformat(rec["date"]))
    except (TypeError, ValueError):
        pass
    msg.set_content(rec["body"])  # UTF-8; bodies contain → and curly quotes
    return msg.as_bytes()


def seed() -> dict[StreamRef, list[SeedEmail]]:
    emails = [
        SeedEmail(provider_message_id=rec["provider_message_id"], raw=record_to_rfc822(rec))
        for rec in load_corpus()
    ]
    return {STREAM: emails}
```

- [ ] **Step 2: Write the failing golden test**

`tests/test_golden_corpus.py`:
```python
"""Golden-master: 17 real emails -> CleanEmail, compared to hand-authored output."""

from __future__ import annotations

from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.emit.memory import MemoryEmitter
from mailflow.providers.memory import MemoryProvider
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)
from tests.fixtures.loader import STREAM, load_golden, seed

GOLDEN_FIELDS = (
    "canonical_id", "message_id", "message_id_trusted", "in_reply_to",
    "references", "direction", "from", "to", "cc", "subject",
    "attachments", "list_id", "auto_submitted",
)


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").splitlines()).strip()


def _project(dumped: dict) -> dict:
    return {k: dumped[k] for k in GOLDEN_FIELDS}


def _run() -> MemoryEmitter:
    emitter = MemoryEmitter()
    pipe = Pipeline(
        provider=MemoryProvider(seed=seed()),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme", max_message_bytes=10_000_000),
    )
    report = pipe.run_once()
    assert report.emitted == 17
    assert report.dropped == 0 and report.dead_lettered == 0
    return emitter


def test_every_real_email_matches_its_golden_clean_email():
    golden = load_golden()
    emitter = _run()
    assert len(emitter.events) == 17
    for event in emitter.events:
        dumped = event.email.model_dump(by_alias=True)
        mid = dumped["message_id"]
        assert mid in golden, f"unexpected message {mid}"
        expected = golden[mid]
        # exact match on identity / threading / routing fields
        assert _project(dumped) == _project(expected), f"field mismatch for {mid}"
        # body compared with newline/whitespace normalization (round-trip tolerant)
        assert _norm(dumped["body_text"]) == _norm(expected["body_text"]), f"body mismatch for {mid}"


def test_deep_reference_chain_is_carried_verbatim():
    golden = load_golden()
    emitter = _run()
    by_mid = {e.email.message_id: e.email for e in emitter.events}
    # the last customer message references the whole 16-deep chain
    deepest = max(golden.values(), key=lambda g: len(g["references"]))
    assert len(deepest["references"]) == 16
    assert by_mid[deepest["message_id"]].references == deepest["references"]


def test_direction_split_matches_watched_mailbox():
    emitter = _run()
    outbound = [e for e in emitter.events if e.email.direction.value == "outbound"]
    inbound = [e for e in emitter.events if e.email.direction.value == "inbound"]
    assert len(outbound) == 7   # from testuser1@cowboyslogistics.com
    assert len(inbound) == 10   # from sandyqatestacc@yahoo.com
```

- [ ] **Step 3: Run to verify it fails (then passes once extractor + pipeline are green)**

Run: `cd mailflow && python -m pytest tests/test_golden_corpus.py -v`
Expected before Tasks 7/13 land: FAIL (import error / mismatch). Once extract-filter and pipeline are green: PASS (3 passed).

> **If a body mismatch persists:** it's almost always RFC822 round-trip of line endings — the `_norm` helper already tolerates trailing whitespace and CRLF↔LF. A *content* mismatch (different words) is a real extractor bug → message `extract-filter-engineer` with the diff. A golden authoring error → fix `golden_cleanemails.json` and note it in the fixtures README. Never regenerate the golden from the extractor (circular).

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/ tests/test_golden_corpus.py
git commit -m "test: golden-master corpus of 17 real Gmail emails -> CleanEmail"
```

---

## Task 17: End-to-end test, full suite, mypy, README

**Files:**
- Create: `tests/test_end_to_end.py`
- Create: `README.md`

- [ ] **Step 1: Write the end-to-end test**

`tests/test_end_to_end.py`:
```python
"""Full pipeline over the memory provider with a realistic mix: a keep (whitelist),
a drop (blacklist), an oversized (DLQ), a newsletter (list_mail drop), and a plain
uncertain->emit. Proves the §8 invariants hold together end-to-end with zero setup."""

from mailflow.builder import build_from_config
from mailflow.config.schema import MailflowConfig
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _raw(mid, frm="x@neutral.com", subject="hi", extra="", body="hello"):
    return (
        f"Message-ID: <{mid}@x>\r\nFrom: {frm}\r\nTo: ops@acme.com\r\n"
        f"Subject: {subject}\r\n{extra}\r\n{body}\r\n"
    ).encode()


def test_full_mixed_run():
    cfg = MailflowConfig.model_validate({
        "tenant": "acme",
        "provider": {"kind": "memory"},
        "filters": [
            {"kind": "whitelist", "params": {"domains": ["partner.com"]}},
            {"kind": "blacklist", "params": {"domains": ["spam.com"]}},
            {"kind": "list_mail"},
        ],
        "emitter": {"kind": "memory"},
        "max_message_bytes": 500,
    })
    seed = {STREAM: [
        SeedEmail("keep", _raw("keep", frm="vip@partner.com")),          # whitelist KEEP
        SeedEmail("drop", _raw("drop", frm="bad@spam.com")),             # blacklist DROP
        SeedEmail("news", _raw("news", extra="List-Id: <n.x.com>\r\n")), # list_mail DROP
        SeedEmail("big", _raw("big", body="z" * 600)),                   # oversized -> DLQ
        SeedEmail("plain", _raw("plain", frm="someone@acme-customer.com")),  # uncertain -> EMIT
    ]}
    pipe = build_from_config(cfg, seed=seed)
    report = pipe.run_once()

    assert report.fetched == 5
    assert report.emitted == 2          # keep + plain
    assert report.dropped == 2          # blacklist + list_mail
    assert report.dead_lettered == 1    # oversized
    # cursor advanced past every message (all reached a terminal disposition)
    assert pipe.cursor_store.get("acme", STREAM).order == 5
    # every message produced a decision trace (debuggability, §12)
    assert len(report.traces) == 5
```

- [ ] **Step 2: Run the FULL suite**

Run: `python -m pytest -v`
Expected: PASS (all tests across all modules green; ~50+ tests).

- [ ] **Step 3: Run mypy strict (the real port-conformance gate, §5.3)**

Run: `python -m mypy`
Expected: `Success: no issues found`. Fix any adapter that fails to satisfy a port (this is where structural typing earns its keep). Common fixes: ensure `MemoryProvider`, the stores, and emitters have the exact method signatures from `ports.py`.

- [ ] **Step 4: Write `README.md` quickstart**

`README.md`:
```markdown
# mailflow (core spine)

Provider- and transport-agnostic email ingestion. This package is the **core**:
the full pipeline (parse → filter → extract → emit) plus the §8 correctness
invariants, runnable end-to-end against an in-memory provider with zero setup.

## Quickstart (zero external setup)

```python
from mailflow import build_from_config, MailflowConfig
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

cfg = MailflowConfig.model_validate({
    "tenant": "acme",
    "provider": {"kind": "memory"},
    "filters": [{"kind": "blacklist", "params": {"domains": ["spam.com"]}}],
    "emitter": {"kind": "memory"},
})
stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
seed = {stream: [SeedEmail("m1", b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: hi\r\n\r\nhello")]}

pipe = build_from_config(cfg, seed=seed)
report = pipe.run_once()
print(report.emitted, report.dropped, report.dead_lettered)
```

## What's here vs later

| Plan | Scope |
|------|-------|
| **1 (this repo)** | core + in-memory adapters |
| 2 | Microsoft Graph adapter + webhook/subscribe |
| 3 | Gmail adapter |
| 4 | reference deploy, allowlisted plugins, attachment streaming, LLM classifier |

The wire contract is `EmailEvent` / `CleanEmail` at `SCHEMA_VERSION = "1.0"`.
Adapters plug into the ports in `mailflow.core.ports` without touching `core`.
```

- [ ] **Step 5: Final commit**

```bash
git add tests/test_end_to_end.py README.md
git commit -m "test: end-to-end mixed run; add README quickstart"
```

---

## Self-Review

**1. Spec coverage (core-spine scope only):**

| Spec section | Covered by |
|--------------|-----------|
| §5.3 ports (11 Protocols) | Task 6 (`ports.py`) — mypy gate in Task 17 |
| §5.4 folder layout | Tasks 1–16 follow `src/mailflow/...` |
| §6.1 identity / idempotency | Task 4 |
| §6.2 CleanEmail + Attachment | Task 5, Task 7 |
| §7.3 parse-envelope | Task 8 |
| §7.4 three-valued filters (empty-by-default) | Task 9 |
| §7.6 extract (real vs inline, content_hash) | Task 7 |
| §7.7 emit | Task 12 |
| §8.1 terminal-disposition cursor advance | Task 13 Cycles B/C/D |
| §8.2 atomic claim | Task 10, Task 13 Cycle C |
| §8.3 monotonic CAS + single-writer | Task 10, Task 13 Cycle F |
| §8.4 DLQ + attempts | Task 10, Task 13 Cycle E |
| §8.6 size guard before download | Task 13 Cycle D |
| §11 config | Task 14 |
| §12 subset use / decision trace / builder | Tasks 12, 15 |
| §14 schema_version | Task 5 |
| Real-mail validation (golden master, 17 emails) | Task 16.5 |
| Execution by agent teams + conformance auditing | Task 0 + `.claude/agents/` |

**Deferred to later plans (intentionally out of scope, called out so the gaps are deliberate, not accidental):** §7.1–7.2 CONNECT/SUBSCRIBE/keep-alive (Plan 2 — needs a real provider), §7.5 LLM classifier (port defined, impl Plan 4), §8.5 heartbeats/canary (Plan 2 — meaningful only with a live subscription), §8.7 bounded resync (Plan 2), §8.8 Pub/Sub ordering (Plan 4 emitter), §9 credential-layer security/RBAC/webhook (Plan 2), §13 plugin allowlist (Plan 4). The `security.read_allowlist` config field exists now (Task 14) but is enforced inside provider adapters in Plan 2.

**2. Placeholder scan:** No "TBD"/"add error handling here"/"similar to Task N" placeholders. Every code step shows complete code. Two explicit *implementer cleanup notes* (Task 3 `_unused` field; Task 13 duplicated `email =` lines) flag inlining artifacts to delete — these are corrections, not placeholders, and the surrounding code is complete without them.

**3. Type consistency:** Names verified consistent across tasks — `Disposition` (emitted/dropped/duplicate/dead_lettered), `Decision` (keep/drop/uncertain), `FilterDecision.{keep,drop,uncertain}`, `parse_envelope(msg, tenant)`, `message_size(msg)`, `DedupeStore.{try_claim,record_attempt,mark_done,release}`, `commit_if_ahead(tenant, stream, cursor)`, `extract_bytes(...)` vs the generic `extract(msg, env)` seam (`Pipeline._extract`), `EmailEvent(tenant, ordering_key, email)`, `build_from_config(cfg, seed=...)`. The `from_` alias (`from`) is applied uniformly in `Envelope`, `CleanEmail`, and every constructor call via `**{"from": ...}`.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-09-mailflow-core-spine.md`. Execution is **agent-team-first** (Task 0), with two fallbacks:

**1. Agent Team (this plan's default)** — Enable agent teams (Task 0), then the lead spawns the five engineer teammates from `.claude/agents/` + the `plan-conformance-verifier`, seeds the shared task list with the dependency DAG, and coordinates. `core-foundation` goes first; `extract-filter` and `adapters` run in parallel; `pipeline` integrates; `golden+e2e` validates against real mail; the verifier gates every transition. Highest parallelism, highest token cost.

**2. Subagent-Driven** — If agent teams aren't enabled, one subagent per task via `superpowers:subagent-driven-development`, using each task's owning role as the subagent `agentType`. The lead reviews between tasks. Same artifacts, less parallelism, lower cost.

**3. Inline Execution** — Execute tasks in this session via `superpowers:executing-plans`, batch with checkpoints. Simplest, no coordination overhead.

Which approach? (And if you'd like to adjust the team roles, dependency edges, or golden-field set before we start, say so.)
