# Consumer-owned filter policy (`on_filtered`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give consumers a connect-level knob, `on_filtered="tag" | "drop"` (default `"tag"`), that decides whether filter-matched email is delivered-with-a-tag or dropped.

**Architecture:** Add two tag fields to `CleanEmail`; add an `on_filtered` field to `PipelineConfig` and fork the `Decision.drop` branch in `Pipeline._do_work` so tag mode extracts/tags/emits instead of discarding; thread the new `connect()` kwarg into `PipelineConfig` for both the memory and Gmail provider paths.

**Tech Stack:** Python 3, pydantic 2.13, pytest 9, mypy --strict.

## Global Constraints

- Use the venv interpreter for everything: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest` and `... -m mypy`.
- mypy is `--strict` and checks the whole importable tree (`packages=["mailflow"]`). **Every commit must leave the tree importable** — land `core/models.py` before code that imports its new fields.
- TDD, one behavior per cycle: failing test → confirm it fails for the stated reason → minimal impl → green → `mypy` clean → commit.
- Commit message trailer on every commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. **Never `git push`.**
- Frozen contract honored here (option A): a tagged-and-delivered message is counted as `emitted` in `RunReport`; do **not** add a new `Disposition` enum value. `record()` counting is untouched.
- `on_filtered` governs **filter matches only**. Duplicates stay suppressed; dead-letters are unchanged.

## File Structure

- `src/mailflow/core/models.py` — add `disposition` + `filter_reason` to `CleanEmail` (Task 1).
- `src/mailflow/core/pipeline.py` — add `on_filtered` to `PipelineConfig`; fork the drop branch in `_do_work` (Task 2).
- `src/mailflow/facade.py` — new `connect(on_filtered=...)` kwarg, threaded into both `PipelineConfig(...)` constructions (Task 3).
- Tests: `tests/test_filter_policy.py` (new) for Tasks 1–2 behavior; extend an integration test in Task 3.

---

### Task 1: Add tag fields to `CleanEmail`

**Files:**
- Modify: `src/mailflow/core/models.py` (imports near line 9; `CleanEmail` fields near lines 225-227)
- Test: `tests/test_filter_policy.py` (create)

**Interfaces:**
- Produces: `CleanEmail.disposition: Literal["emitted", "filtered"] = "emitted"` and `CleanEmail.filter_reason: str = ""`. Task 2 sets these.

- [ ] **Step 1: Write the failing test**

Create `tests/test_filter_policy.py`:

```python
from mailflow.core.models import CleanEmail


def test_cleanemail_disposition_defaults_to_emitted():
    email = CleanEmail(canonical_id="c1", provider="memory", provider_message_id="m1",
                       provider_stream_id="s1")
    assert email.disposition == "emitted"
    assert email.filter_reason == ""


def test_cleanemail_can_be_marked_filtered():
    email = CleanEmail(canonical_id="c1", provider="memory", provider_message_id="m1",
                       provider_stream_id="s1")
    email.disposition = "filtered"
    email.filter_reason = "list/auto-submitted header present"
    assert email.disposition == "filtered"
    assert email.filter_reason == "list/auto-submitted header present"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_filter_policy.py -v`
Expected: FAIL — `AttributeError`/validation: `CleanEmail` has no field `disposition`.

- [ ] **Step 3: Write minimal implementation**

In `src/mailflow/core/models.py`, add the `Literal` import to the existing typing-free import block (after line 7, `from functools import total_ordering`):

```python
from typing import Literal
```

Then in `CleanEmail`, immediately after `matched_filter: str = ""` (currently line 226), add:

```python
    disposition: Literal["emitted", "filtered"] = "emitted"
    filter_reason: str = ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_filter_policy.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: mypy clean**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/mailflow/core/models.py tests/test_filter_policy.py
git commit -m "feat(models): add CleanEmail.disposition + filter_reason tag fields

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `on_filtered` config + tag fork in the pipeline

**Files:**
- Modify: `src/mailflow/core/pipeline.py` (`PipelineConfig` at lines 58-63; `_do_work` `Decision.drop` branch at lines 206-212)
- Test: `tests/test_filter_policy.py` (extend)

**Interfaces:**
- Consumes: `CleanEmail.disposition`, `CleanEmail.filter_reason` (Task 1).
- Produces: `PipelineConfig.on_filtered: Literal["tag", "drop"] = "tag"`. In tag mode, `_do_work` returns `Disposition.emitted` for a filter-matched message and the emitted `CleanEmail` has `disposition="filtered"`, `matched_filter=<name>`, `filter_reason=<reason>`.

Reference — the current `_do_work` drop branch to replace (lines 206-212):

```python
        if decision.decision is Decision.drop:
            self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
            self._record(report, self._trace(
                env.canonical_id, msg, Disposition.dropped, "filter",
                matched_filter=decision.filter_name, reason=decision.reason,
            ))
            return Disposition.dropped
```

The emit tail it should reuse in tag mode already exists further down the same method (lines 234-247): build `EmailEvent`, `self.emitter.emit(event)`, `self.dedupe_store.mark_done(...)`, `self._record(... Disposition.emitted, "emit", ...)`, `return Disposition.emitted`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_filter_policy.py`:

```python
import pytest

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Disposition
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import AutoSubmittedFilter
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.core.models import StreamRef
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.clean import ThinContentCleaner
from mailflow.stores.memory import (
    InMemoryCursorStore, InMemoryDedupeStore, InMemoryBlobStore,
)


AUTO_SUBMITTED_RAW = (
    b"From: mailer@example.com\r\n"
    b"To: me@example.com\r\n"
    b"Subject: Out of office\r\n"
    b"Auto-Submitted: auto-replied\r\n"
    b"Message-ID: <a1@example.com>\r\n\r\n"
    b"I am away.\r\n"
)


def _pipeline(on_filtered):
    stream = StreamRef(mailbox="me@example.com", folder=None)
    provider = MemoryProvider(seed={stream: [SeedEmail(provider_message_id="m1",
                                                        raw=AUTO_SUBMITTED_RAW)]})
    emitter = MemoryEmitter()
    return provider, emitter, Pipeline(
        provider=provider,
        parser=MimeEnvelopeParser(),
        filters=FilterChain([AutoSubmittedFilter()]),
        extractor=MimeExtractor(),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        cleaner=ThinContentCleaner(),
        config=PipelineConfig(tenant="acme", on_filtered=on_filtered),
    )


def test_drop_mode_suppresses_filtered_email():
    _provider, emitter, pipe = _pipeline("drop")
    report = pipe.run_once()
    assert report.dropped == 1
    assert report.emitted == 0
    assert emitter.events == []


def test_tag_mode_delivers_filtered_email_with_tag():
    _provider, emitter, pipe = _pipeline("tag")
    report = pipe.run_once()
    assert report.emitted == 1
    assert report.dropped == 0
    email = emitter.events[0].email
    assert email.disposition == "filtered"
    assert email.matched_filter  # the filter name is set
    assert email.filter_reason   # a human reason is set


def test_tag_mode_is_the_default():
    stream = StreamRef(mailbox="me@example.com", folder=None)
    cfg = PipelineConfig(tenant="acme")
    assert cfg.on_filtered == "tag"


def test_emitted_email_disposition_stays_emitted():
    # a message that passes all filters keeps disposition == "emitted"
    stream = StreamRef(mailbox="me@example.com", folder=None)
    normal = (b"From: a@example.com\r\nTo: me@example.com\r\n"
              b"Subject: hi\r\nMessage-ID: <n1@example.com>\r\n\r\nhello\r\n")
    provider = MemoryProvider(seed={stream: [SeedEmail(provider_message_id="n1", raw=normal)]})
    emitter = MemoryEmitter()
    pipe = Pipeline(
        provider=provider, parser=MimeEnvelopeParser(),
        filters=FilterChain([AutoSubmittedFilter()]), extractor=MimeExtractor(),
        emitter=emitter, dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), cleaner=ThinContentCleaner(),
        config=PipelineConfig(tenant="acme", on_filtered="tag"),
    )
    pipe.run_once()
    assert emitter.events[0].email.disposition == "emitted"
```

> Before running: if any imported name above (e.g. `MemoryEmitter.events`, `InMemoryBlobStore`, `SeedEmail(raw=...)`, `Pipeline.run_once`) does not match the real symbol, grep for the correct name and fix the test import — do not weaken the assertion. Confirm with:
> `grep -rn "class MemoryEmitter\|class InMemory\|class SeedEmail\|def run_once" src/mailflow`

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_filter_policy.py -v`
Expected: `test_tag_mode_*` and `test_tag_mode_is_the_default` FAIL — `PipelineConfig` has no field `on_filtered` (and tag mode currently drops).

- [ ] **Step 3: Add the config field**

In `src/mailflow/core/pipeline.py`, add the `Literal` import near the top typing imports:

```python
from typing import Literal
```

Then extend `PipelineConfig` (after `done_ttl_seconds`, line 63):

```python
    on_filtered: Literal["tag", "drop"] = "tag"
```

- [ ] **Step 4: Fork the drop branch in `_do_work`**

Replace the `Decision.drop` branch (lines 206-212) with:

```python
        if decision.decision is Decision.drop:
            if self.config.on_filtered == "drop":
                self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
                self._record(report, self._trace(
                    env.canonical_id, msg, Disposition.dropped, "filter",
                    matched_filter=decision.filter_name, reason=decision.reason,
                ))
                return Disposition.dropped
            # on_filtered == "tag": deliver the matched message, tagged. Fall through to
            # the normal extract -> emit path, but stamp the filter tag on the email below.
            _tag = (decision.filter_name, decision.reason)
        else:
            _tag = None
```

Then, in the emit path of the same method, immediately after `email = self._extract(msg, env)` (line 218) and the existing `email.matched_filter = decision.filter_name` (line 221), add the tag stamp:

```python
        if _tag is not None:
            email.disposition = "filtered"
            email.matched_filter, email.filter_reason = _tag
```

(Leave `Disposition.emitted` counting and the `"emit"` trace as-is — a tagged message counts as one `emitted`, per the frozen contract. `matched_filter` on the trace stays whatever the emit path already records.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_filter_policy.py -v`
Expected: PASS (all tests, including the `drop`-mode regression guard).

- [ ] **Step 6: Full suite + mypy**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q && /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: whole suite green; `Success: no issues found`. (Existing filter tests that assumed drop-by-default at the pipeline level, if any, should be checked — the pipeline default is now `tag`. Fix any such test by passing `on_filtered="drop"` explicitly where it means to assert drop behavior.)

- [ ] **Step 7: Commit**

```bash
git add src/mailflow/core/pipeline.py tests/test_filter_policy.py
git commit -m "feat(pipeline): on_filtered tag/drop policy; tag delivers matched mail

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Expose `on_filtered` on `connect()`

**Files:**
- Modify: `src/mailflow/facade.py` (`connect` signature ~line 216-233; memory `PipelineConfig(tenant=tenant)` at line 274; Gmail path — thread into the live builder / its `PipelineConfig`)
- Test: `tests/test_filter_policy.py` (extend with a `connect(...)`-level test)

**Interfaces:**
- Consumes: `PipelineConfig.on_filtered` (Task 2).
- Produces: `connect(..., on_filtered: Literal["tag","drop"] = "tag")` reaching the pipeline for both `provider="memory"` and `provider="gmail"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_filter_policy.py`:

```python
from mailflow import connect
from mailflow.providers.memory import SeedEmail
from mailflow.core.models import StreamRef


def test_connect_memory_tag_mode_delivers_filtered(monkeypatch):
    stream = StreamRef(mailbox="me@example.com", folder=None)
    seed = {stream: [SeedEmail(provider_message_id="m1", raw=AUTO_SUBMITTED_RAW)]}
    from mailflow.filters.deterministic import AutoSubmittedFilter
    mf = connect("memory", seed=seed, filters=[AutoSubmittedFilter()])  # default on_filtered="tag"
    emails = mf.fetch_new()
    assert len(emails) == 1
    assert emails[0].disposition == "filtered"


def test_connect_memory_drop_mode_suppresses_filtered():
    stream = StreamRef(mailbox="me@example.com", folder=None)
    seed = {stream: [SeedEmail(provider_message_id="m1", raw=AUTO_SUBMITTED_RAW)]}
    from mailflow.filters.deterministic import AutoSubmittedFilter
    mf = connect("memory", seed=seed, filters=[AutoSubmittedFilter()], on_filtered="drop")
    assert mf.fetch_new() == []
```

> If `connect` builds the memory filter list differently (e.g. `filters=[{"kind": "auto_submitted"}]`), grep the registry for the built-in kind name and use that form instead:
> `grep -rn "auto_submitted\|AutoSubmitted\|def build_filter" src/mailflow`

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_filter_policy.py::test_connect_memory_drop_mode_suppresses_filtered -v`
Expected: FAIL — `connect()` got an unexpected keyword argument `on_filtered`.

- [ ] **Step 3: Add the kwarg and thread it**

In `src/mailflow/facade.py`:

a) Add `Literal` to the typing import (line 24) if absent, and add the parameter to `connect` (in the keyword-only block, e.g. after `attachments=...`, line 232):

```python
    on_filtered: Literal["tag", "drop"] = "tag",
```

b) Memory path — change line 274 from `config=PipelineConfig(tenant=tenant),` to:

```python
            config=PipelineConfig(tenant=tenant, on_filtered=on_filtered),
```

c) Gmail path — pass `on_filtered` into `_build_gmail_live(...)` (add it to the call at lines 283-290 and to the function's signature at 349-364), then thread it into the `PipelineConfig` that `run_service` builds. Grep to find where the live path constructs `PipelineConfig`:

```bash
grep -rn "PipelineConfig(" src/mailflow/adapters/gmail
```

Add an `on_filtered: Literal["tag","drop"] = "tag"` parameter along that call chain (`_build_gmail_live` → `run_service` → the `PipelineConfig(...)` construction) and pass the value through. Keep the default `"tag"` at each hop so partial callers are unaffected.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_filter_policy.py -v`
Expected: PASS (all).

- [ ] **Step 5: Full suite + mypy**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q && /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: green; `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/mailflow/facade.py tests/test_filter_policy.py
git commit -m "feat(connect): expose on_filtered=tag|drop (default tag) for memory + gmail

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Docs — record the default flip

**Files:**
- Modify: `README.md` (connect options / filtering section) and `docs/qa-findings.md` if a finding tracked the silent-drop gap.

**Interfaces:** none (documentation only).

- [ ] **Step 1: Update the connect docs**

Document `on_filtered` in the `connect()` options: default `"tag"` delivers filter-matched mail with `disposition="filtered"` + `filter_reason`; `"drop"` restores prior suppression. Include the branch-on-disposition snippet:

```python
for email in mf.stream():
    if email.disposition == "filtered":
        archive_but_dont_process(email)
    else:
        handle(email)
```

Add a **Breaking change** note: filters now tag-and-deliver by default instead of dropping; pass `on_filtered="drop"` to restore old behavior.

- [ ] **Step 2: Commit**

```bash
git add README.md docs/qa-findings.md
git commit -m "docs: document on_filtered and the tag-by-default behavior change

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- `on_filtered` connect knob, default `"tag"` → Task 3 (+ Task 2 config default). ✅
- `disposition` + `filter_reason` on `CleanEmail` → Task 1. ✅
- Pipeline tag fork + reuse of the emit path → Task 2. ✅
- Counting option A (count as `emitted`, no new enum value) → Task 2 Step 4 + Global Constraints. ✅
- Cursor/dedupe unchanged (emit path handles both) → Task 2 (reuses existing `mark_done`; cursor advances on the emit disposition). ✅
- `fields=` projectability → automatic (new `CleanEmail` fields); covered implicitly, no extra task needed. ✅
- Backward-compat default flip called out → Task 2 Step 6 note + Task 4. ✅
- Non-goals (durable dispositions/DLQ/token rotation, duplicate/dead-letter handling) → explicitly out of scope; no tasks, by design. ✅
- Open question (param name `on_filtered`) → resolved to `on_filtered` per user; used consistently. ✅

**Placeholder scan:** No TBD/TODO. The two "grep to confirm the real symbol name" notes are deliberate guards against drift in test imports / the Gmail `PipelineConfig` call site, each with the exact grep to run and instruction not to weaken assertions — not placeholders for missing content.

**Type consistency:** `disposition: Literal["emitted","filtered"]` and `filter_reason: str` used identically in Tasks 1–3; `on_filtered: Literal["tag","drop"]` default `"tag"` identical across `PipelineConfig`, `connect`, and the Gmail call chain.
