# Config Hardening & Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Close three operational-hardening gaps that ship today as silent no-ops or missing safety rails: (1) a startup **port-conformance smoke check** so a mis-wired component fails fast at construction with a named error instead of an `AttributeError` mid-run; (2) **observability** — dependency-free structured logging at every disposition (never logging email bodies), a metrics counter seam, and a `health()` reachability probe — plus restoring the `RunReport` test deleted in `ba69e7b`; (3) replace **dead security config** with real behavior: make `verify_scope_on_startup` actually verify the granted Gmail OAuth scope at startup, and **delete** `read_allowlist` (unenforced, with documented "fail-closed" semantics that are inverted from real behavior — enforcement was already punted to Plan 2).

**Architecture:** All three tasks bolt onto existing seams without changing frozen contracts.
- The conformance check lives in `core/pipeline.py` (`Pipeline.__init__`) so it catches **every** construction path — `build_from_config`, `connect()`, the Gmail composition root, and direct construction — by `isinstance`-checking each injected component against its `@runtime_checkable` Protocol from `core/ports.py`. It raises the existing `ConfigError`.
- Observability lives in `core/observability.py` next to `RunReport`/`DecisionTrace`: a `RunReport.counters()` metrics seam, a `health()` function + `HealthReport` model that probes the stores via their ports, and structured `logging` log points wired into `core/pipeline.py` at each disposition. Logging only ever emits `DecisionTrace` fields (canonical_id / disposition / stage / reason), which by construction contain **no body bytes** — PII scrubbing stays deferred, but bodies are never logged.
- Security: `verify_scope_on_startup` becomes real in the Gmail OAuth path (`adapters/gmail/live.py`) — `OAuthTokenProvider` grows a `verify_scopes()` method, `run_service` gains a `verify_scope` flag, and `facade.connect()` threads `verify_scope_on_startup` (defaulting from `SecurityConfig`, making the schema field load-bearing). `read_allowlist` is deleted from `config/schema.py` and its doc references.

**Tech Stack:** Python 3.12, pydantic 2.13, pytest 9, mypy (strict). Stdlib `logging` only — **no** new dependencies. PyYAML is NOT installed: the config loader falls back to JSON, so keep any config fixtures JSON-compatible. Run tests/types with the venv interpreter:
- `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest`
- `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`

Every commit must leave the importable tree green under `mypy --strict` and ends with:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## Task 1 — Port-conformance smoke check + fail-fast

The missing half of config validation. `validate()` (`config/loader.py:54-75`) checks config *kinds*; nothing checks that an **injected** component (via `overrides=`, the `connect()` seams, or a hand-built `Pipeline`) actually satisfies its port. A bad component currently surfaces as a late `AttributeError` mid-run. Add an `isinstance`-against-Protocol smoke check in `Pipeline.__init__` that fails fast with a `ConfigError` naming the offending role + class + port.

**Files:**
- `src/mailflow/core/pipeline.py` — `Pipeline.__init__` at lines `51-78`; ports already imported at `27-37` (add `Filter`); `ConfigError` import to add (currently imports `AuthError, PermanentError, TransientError` from `mailflow.core.errors` at line `14`). `MimeExtractor` already imported at line `38`; `FilterChain` at line `39`.
- `src/mailflow/core/ports.py` — read-only reference: the `@runtime_checkable` Protocols (`MailboxProvider:43`, `EnvelopeParser:60`, `Filter:66`, `Classifier:73`, `ContentExtractor:79`, `Emitter:85`, `CursorStore:91`, `DedupeStore:97`, `BlobStore:113`, `ContentCleaner:128`). Each runtime_checkable Protocol exposes `__protocol_attrs__` (confirmed: `Emitter.__protocol_attrs__ == {'emit'}`).
- `tests/test_port_conformance.py` — NEW.

- [ ] **Step 1: Failing test — a non-conforming emitter is rejected at construction.**
  Create `tests/test_port_conformance.py`. Reuse the in-memory wiring shape from the (deleted) pipeline test. A minimal helper builds a valid `Pipeline` from in-memory parts, and one test swaps in a junk emitter:
  ```python
  """Task 1: Pipeline.__init__ fails fast when an injected component does not
  satisfy its @runtime_checkable port (the missing half of config validation)."""

  from __future__ import annotations

  import pytest

  from mailflow.core.errors import ConfigError
  from mailflow.core.pipeline import Pipeline, PipelineConfig
  from mailflow.emit.memory import MemoryEmitter
  from mailflow.extract.envelope import MimeEnvelopeParser
  from mailflow.extract.mime import MimeExtractor
  from mailflow.filters.chain import FilterChain
  from mailflow.filters.deterministic import FunctionFilter
  from mailflow.providers.memory import MemoryProvider
  from mailflow.stores.memory import (
      InMemoryBlobStore,
      InMemoryCursorStore,
      InMemoryDedupeStore,
  )


  def _kwargs(**overrides: object) -> dict[str, object]:
      base: dict[str, object] = dict(
          provider=MemoryProvider(seed={}),
          parser=MimeEnvelopeParser(),
          filters=FilterChain([]),
          extractor=MimeExtractor(),
          emitter=MemoryEmitter(),
          dlq_emitter=MemoryEmitter(),
          cursor_store=InMemoryCursorStore(),
          dedupe_store=InMemoryDedupeStore(),
          blob_store=InMemoryBlobStore(),
          config=PipelineConfig(tenant="acme"),
      )
      base.update(overrides)
      return base


  class _NotAnEmitter:
      """No .emit method -> does not satisfy the Emitter port."""


  def test_valid_in_memory_pipeline_constructs() -> None:
      Pipeline(**_kwargs())  # type: ignore[arg-type]


  def test_bad_emitter_rejected_with_named_error() -> None:
      with pytest.raises(ConfigError) as exc:
          Pipeline(**_kwargs(emitter=_NotAnEmitter()))  # type: ignore[arg-type]
      msg = str(exc.value)
      assert "emitter" in msg and "Emitter" in msg


  def test_bad_filter_in_chain_rejected() -> None:
      with pytest.raises(ConfigError) as exc:
          Pipeline(**_kwargs(filters=FilterChain([object()])))  # type: ignore[arg-type,list-item]
      assert "filter" in str(exc.value) and "Filter" in str(exc.value)


  def test_mime_extractor_accepted_despite_no_extract_method() -> None:
      # MimeExtractor exposes extract_bytes (not the port's extract); the seam allows it.
      Pipeline(**_kwargs(extractor=MimeExtractor()))  # type: ignore[arg-type]


  def test_none_classifier_and_cleaner_are_allowed() -> None:
      Pipeline(**_kwargs(classifier=None, cleaner=None))  # type: ignore[arg-type]
  ```

- [ ] **Step 2: Run it — confirm it fails for the right reason.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_port_conformance.py -q`
  Expected: `test_bad_emitter_rejected_with_named_error` and `test_bad_filter_in_chain_rejected` FAIL — no `ConfigError` is raised today (construction succeeds; the junk component only blows up later mid-run). The other three should pass (valid construction is unaffected). This confirms the gap.

- [ ] **Step 3: Implement the smoke check in `Pipeline.__init__`.**
  In `src/mailflow/core/pipeline.py`:
  - Add `ConfigError` to the errors import (line 14): `from mailflow.core.errors import AuthError, ConfigError, PermanentError, TransientError`.
  - Add `Filter` to the ports import block (lines 27-37).
  - Add a module-level helper above the `Pipeline` class:
    ```python
    def _assert_port(component: object, port: type, role: str) -> None:
        """Fail fast if `component` does not structurally satisfy `port` (A-port check).
        runtime_checkable isinstance only verifies method-name presence — cheap, and run
        once at construction. mypy strict is the real conformance gate; this catches the
        injection seams (overrides=, connect(), hand-built pipelines) mypy can't see."""
        if isinstance(component, port):
            return
        attrs: frozenset[str] = getattr(port, "__protocol_attrs__", frozenset())
        missing = sorted(a for a in attrs if not hasattr(component, a))
        raise ConfigError(
            f"{role} component {type(component).__name__!r} does not satisfy the "
            f"{port.__name__} port (missing: {missing or 'unknown'})"
        )
    ```
  - At the **top** of `Pipeline.__init__` (right after the signature, before the `self.* = ` assignments), validate each dependency. The extractor honors the existing seam (`MimeExtractor` is allowed as a concrete type even though it lacks the port's `extract`); classifier/cleaner are optional:
    ```python
    _assert_port(provider, MailboxProvider, "provider")
    _assert_port(parser, EnvelopeParser, "parser")
    for _f in filters.filters:
        _assert_port(_f, Filter, "filter")
    if not isinstance(extractor, (ContentExtractor, MimeExtractor)):
        _assert_port(extractor, ContentExtractor, "extractor")
    _assert_port(emitter, Emitter, "emitter")
    _assert_port(dlq_emitter, Emitter, "dlq_emitter")
    _assert_port(cursor_store, CursorStore, "cursor_store")
    _assert_port(dedupe_store, DedupeStore, "dedupe_store")
    _assert_port(blob_store, BlobStore, "blob_store")
    if classifier is not None:
        _assert_port(classifier, Classifier, "classifier")
    if cleaner is not None:
        _assert_port(cleaner, ContentCleaner, "cleaner")
    ```

- [ ] **Step 4: Green + full suite + types.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_port_conformance.py -q` → all pass.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q` → confirm no regression (the live Gmail composition + facade + builder all build `Pipeline` and must still construct; the conformance check is satisfied by the real components).
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` → clean.

- [ ] **Step 5: Commit.**
  ```
  feat(hardening): fail-fast port-conformance smoke check in Pipeline.__init__

  Pipeline now isinstance-checks every injected component against its
  @runtime_checkable port and raises ConfigError naming the offending
  role/class/port, instead of a late AttributeError mid-run. Covers all
  construction paths (overrides=, connect(), composition root, direct).

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

---

## Task 2 — Observability: structured logging + metrics + health check

Today only `RunReport`/`DecisionTrace` exist; there is zero `logging`, no metrics seam, and no health probe. Add all three dependency-free, and restore the `RunReport` count test deleted in `ba69e7b`. **Never log email bodies** — log points emit only `DecisionTrace` fields (which carry no body bytes); PII scrubbing stays deferred.

**Files:**
- `src/mailflow/core/observability.py` — `RunReport` at `29-53` (counters `fetched/emitted/dropped/duplicates/dead_lettered`); currently imports only `Disposition` from `mailflow.core.models`. Add `RunReport.counters()`, a `HealthReport` model, and a `health()` function.
- `src/mailflow/core/pipeline.py` — add `logging` log points routed through one helper so each disposition is logged once (dispositions populated at `108`, `134-137`, `158-161`, `228`); add a `Pipeline.health()` convenience.
- `src/mailflow/facade.py` — `Mailflow.__init__` at `107-126`; add `dedupe_store`/`blob_store` to the handle + a `Mailflow.health()`; both `connect()` branches (`240-256`, `258-273`) already have the stores in scope.
- `tests/core/test_observability.py` — RECREATE (deleted in `ba69e7b`) + extend.
- `tests/test_health.py` — NEW.

- [ ] **Step 1: Restore the deleted `RunReport` coverage (regression guard).**
  Recreate `tests/core/test_observability.py` with the exact assertions removed in `ba69e7b` (the DLQ-owns-`dead_lettered` contract is load-bearing — see CLAUDE.md):
  ```python
  """Restored from ba69e7b + new metrics seam. RunReport counts dispositions;
  dead_lettered is owned solely by add_dead_letter (record() never counts it)."""

  from __future__ import annotations

  from mailflow.core.models import Disposition
  from mailflow.core.observability import DeadLetter, DecisionTrace, RunReport


  def test_run_report_counts_and_accumulates() -> None:
      report = RunReport()
      report.record(DecisionTrace(
          canonical_id="c1", tenant="acme", stream="ops@acme.com:Inbox",
          disposition=Disposition.emitted, stage="emit",
      ))
      report.record(DecisionTrace(
          canonical_id="c2", tenant="acme", stream="ops@acme.com:Inbox",
          disposition=Disposition.dropped, stage="filter", matched_filter="blacklist",
      ))
      report.add_dead_letter(
          DeadLetter(canonical_id="c3", reason="oversized", provider_message_id="m3")
      )
      assert report.emitted == 1
      assert report.dropped == 1
      assert report.dead_lettered == 1
      assert len(report.traces) == 2
      assert report.dlq[0].reason == "oversized"


  def test_record_does_not_double_count_dead_lettered() -> None:
      # The pipeline calls add_dead_letter() AND record(dead_lettered) as a pair;
      # record() must NOT also count it, or one poison message would count twice.
      report = RunReport()
      report.add_dead_letter(
          DeadLetter(canonical_id="c1", reason="poison", provider_message_id="m1")
      )
      report.record(DecisionTrace(
          canonical_id="c1", tenant="acme", stream="s", stage="dlq",
          disposition=Disposition.dead_lettered, reason="poison",
      ))
      assert report.dead_lettered == 1
      assert len(report.traces) == 1
  ```
  Run `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/core/test_observability.py -q` → both PASS immediately (characterization of unchanged `RunReport`). Commit this restoration on its own:
  ```
  test(observability): restore RunReport count coverage deleted in ba69e7b

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

- [ ] **Step 2: Failing test — metrics counter seam (`RunReport.counters()`).**
  Append to `tests/core/test_observability.py`:
  ```python
  def test_counters_exposes_disposition_metrics() -> None:
      report = RunReport()
      report.fetched = 5
      report.record(DecisionTrace(
          canonical_id="c1", tenant="t", stream="s",
          disposition=Disposition.emitted, stage="emit",
      ))
      report.add_dead_letter(
          DeadLetter(canonical_id="c2", reason="x", provider_message_id="m2")
      )
      assert report.counters() == {
          "fetched": 5, "emitted": 1, "dropped": 0,
          "duplicate": 0, "dead_lettered": 1,
      }
  ```
  Run it → FAILS with `AttributeError: 'RunReport' object has no attribute 'counters'`.

- [ ] **Step 3: Implement `RunReport.counters()`.**
  In `src/mailflow/core/observability.py`, add a method to `RunReport` (note the key is `"duplicate"`, the canonical `Disposition` name; the field is `duplicates`):
  ```python
  def counters(self) -> dict[str, int]:
      """Dependency-free metrics seam: disposition counters keyed by the canonical
      Disposition names, for a metrics exporter to scrape after run_once()."""
      return {
          "fetched": self.fetched,
          "emitted": self.emitted,
          "dropped": self.dropped,
          "duplicate": self.duplicates,
          "dead_lettered": self.dead_lettered,
      }
  ```
  Run Step-2 test → green.

- [ ] **Step 4: Failing test — `health()` reachability probe.**
  Create `tests/test_health.py`:
  ```python
  """Task 2c: health() probes core store dependencies via their ports and reports
  reachability without corrupting state."""

  from __future__ import annotations

  from typing import Iterator

  import pytest

  from mailflow.core.observability import HealthReport, health
  from mailflow.stores.memory import (
      InMemoryBlobStore,
      InMemoryCursorStore,
      InMemoryDedupeStore,
  )


  def test_health_all_reachable() -> None:
      report = health(
          cursor_store=InMemoryCursorStore(),
          dedupe_store=InMemoryDedupeStore(),
          blob_store=InMemoryBlobStore(),
      )
      assert isinstance(report, HealthReport)
      assert report.healthy is True
      assert report.checks["cursor"] == "ok"
      assert report.checks["dedupe"] == "ok"
      assert report.checks["blob"] == "ok"


  class _BrokenCursor:
      def get(self, tenant, stream):  # type: ignore[no-untyped-def]
          raise RuntimeError("db unreachable")

      def commit_if_ahead(self, tenant, stream, cursor):  # type: ignore[no-untyped-def]
          return False


  def test_health_reports_unreachable_dependency() -> None:
      report = health(
          cursor_store=_BrokenCursor(),
          dedupe_store=InMemoryDedupeStore(),
          blob_store=InMemoryBlobStore(),
      )
      assert report.healthy is False
      assert "db unreachable" in report.checks["cursor"]


  def test_health_does_not_corrupt_dedupe_state() -> None:
      # the probe must claim+release so a real key can still be claimed afterwards.
      dedupe = InMemoryDedupeStore()
      health(cursor_store=InMemoryCursorStore(), dedupe_store=dedupe,
             blob_store=InMemoryBlobStore())
      assert dedupe.try_claim("__healthcheck__", 1) is True  # probe released it
  ```
  Run `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_health.py -q` → FAILS (`ImportError: cannot import name 'HealthReport'` / `health`).

- [ ] **Step 5: Implement `HealthReport` + `health()`.**
  In `src/mailflow/core/observability.py`:
  - Extend the models import: `from mailflow.core.models import Disposition, StreamRef`.
  - Add a ports import (no cycle — `ports.py` does not import `observability`):
    `from mailflow.core.ports import BlobStore, CursorStore, DedupeStore`.
  - Add:
    ```python
    _PROBE_TENANT = "__healthcheck__"
    _PROBE_KEY = "__healthcheck__"
    _PROBE_STREAM = StreamRef(mailbox="__healthcheck__", folder=None)


    class HealthReport(BaseModel):
        healthy: bool
        checks: dict[str, str] = Field(default_factory=dict)


    def health(
        *, cursor_store: CursorStore, dedupe_store: DedupeStore, blob_store: BlobStore
    ) -> HealthReport:
        """Probe the core store dependencies via their ports. Reads are harmless; the
        dedupe probe claims+releases a reserved key so it never corrupts real state. A
        deep blob write-probe is deferred — blob is a structural (port) check here."""
        checks: dict[str, str] = {}

        try:
            cursor_store.get(_PROBE_TENANT, _PROBE_STREAM)  # read-only liveness
            checks["cursor"] = "ok"
        except Exception as exc:  # noqa: BLE001 - any failure means unreachable
            checks["cursor"] = f"error: {exc}"

        try:
            if dedupe_store.try_claim(_PROBE_KEY, 1):
                dedupe_store.release(_PROBE_KEY)  # leave no trace
            checks["dedupe"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["dedupe"] = f"error: {exc}"

        checks["blob"] = "ok" if isinstance(blob_store, BlobStore) \
            else "error: does not satisfy BlobStore port"

        return HealthReport(healthy=all(v == "ok" for v in checks.values()), checks=checks)
    ```
  Run Step-4 tests → green.

- [ ] **Step 6: Failing test — pipeline logs each disposition, never the body.**
  Add `tests/test_pipeline_logging.py`:
  ```python
  """Task 2a: the pipeline emits a structured log line per disposition and NEVER
  logs the email body (PII scrubbing is deferred, but bodies are never logged)."""

  from __future__ import annotations

  import logging

  from mailflow.core.models import StreamRef
  from mailflow.core.pipeline import Pipeline, PipelineConfig
  from mailflow.emit.memory import MemoryEmitter
  from mailflow.extract.envelope import MimeEnvelopeParser
  from mailflow.extract.mime import MimeExtractor
  from mailflow.filters.chain import FilterChain
  from mailflow.providers.memory import MemoryProvider, SeedEmail
  from mailflow.stores.memory import (
      InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore,
  )

  STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")
  SECRET_BODY = "TOP-SECRET-BODY-DO-NOT-LOG"


  def _raw() -> bytes:
      return (
          "Message-ID: <m1@example.com>\r\nFrom: alice@partner.com\r\n"
          f"To: ops@acme.com\r\nSubject: hi\r\n\r\n{SECRET_BODY}\r\n"
      ).encode()


  def _pipeline() -> Pipeline:
      return Pipeline(
          provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", _raw())]}),
          parser=MimeEnvelopeParser(), filters=FilterChain([]),
          extractor=MimeExtractor(), emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
          cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
          blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="acme"),
      )


  def test_emitted_disposition_is_logged(caplog) -> None:  # type: ignore[no-untyped-def]
      with caplog.at_level(logging.INFO, logger="mailflow.pipeline"):
          _pipeline().run_once()
      records = [r for r in caplog.records if r.name == "mailflow.pipeline"]
      assert any("emitted" in r.getMessage() for r in records)


  def test_body_is_never_logged(caplog) -> None:  # type: ignore[no-untyped-def]
      with caplog.at_level(logging.DEBUG, logger="mailflow.pipeline"):
          _pipeline().run_once()
      assert SECRET_BODY not in caplog.text
  ```
  Run → `test_emitted_disposition_is_logged` FAILS (no log records emitted).

- [ ] **Step 7: Implement structured logging in the pipeline.**
  In `src/mailflow/core/pipeline.py`:
  - Add at top: `import logging` and (after imports) `_log = logging.getLogger("mailflow.pipeline")`.
  - Route every `report.record(...)` through one body-free helper. Add a method:
    ```python
    def _record(self, report: RunReport, trace: DecisionTrace) -> None:
        report.record(trace)
        _log.info(
            "disposition=%s stage=%s canonical_id=%s stream=%s "
            "matched_filter=%s reason=%s",
            trace.disposition.value, trace.stage, trace.canonical_id,
            trace.stream, trace.matched_filter, trace.reason,
        )
    ```
    Note: only `DecisionTrace` scalar fields are logged — no `CleanEmail`, no body, no headers.
  - Replace the three in-pipeline `report.record(self._trace(...))` calls (duplicate at ~108, dropped at ~134, emitted at ~158) with `self._record(report, self._trace(...))`.
  - In `_dead_letter` (~228), replace `report.record(self._trace(...dead_lettered...))` with `self._record(report, self._trace(...))`. (The `add_dead_letter()` call stays exactly as-is — the DLQ-owns-the-count contract is unchanged; `_record` only adds a log line alongside the existing `record()`.)

- [ ] **Step 8: Add `Pipeline.health()` convenience.**
  In `src/mailflow/core/pipeline.py`, import `from mailflow.core.observability import DeadLetter, DecisionTrace, HealthReport, RunReport, health` (extend the existing line 26 import) and add:
  ```python
  def health(self) -> HealthReport:
      """Reachability of this pipeline's core store dependencies (ops probe)."""
      return health(
          cursor_store=self.cursor_store,
          dedupe_store=self.dedupe_store,
          blob_store=self.blob_store,
      )
  ```
  (The method name `health` shadows the imported function inside the method body only at call sites prefixed `self.` — to avoid the name clash, import the function under an alias: `from mailflow.core.observability import health as _health` and call `_health(...)` inside the method. Use the alias.)

- [ ] **Step 9: Expose `Mailflow.health()` at the facade (user-facing ops surface).**
  In `src/mailflow/facade.py`:
  - Import `from mailflow.core.observability import HealthReport, health as _health` and `from mailflow.core.ports import BlobStore, DedupeStore` (extend the existing ports import at line 29).
  - Add `dedupe_store: DedupeStore | None = None` and `blob_store: BlobStore | None = None` params to `Mailflow.__init__` (lines 107-118) and store them (`self._dedupe_store = dedupe_store`, `self._blob_store = blob_store`).
  - Add:
    ```python
    def health(self) -> HealthReport:
        """Reachability of the cursor/dedupe/blob stores backing this handle."""
        if self._dedupe_store is None or self._blob_store is None:
            raise RuntimeError("health() requires the store-backed handle")
        return _health(
            cursor_store=self.cursor_store,
            dedupe_store=self._dedupe_store,
            blob_store=self._blob_store,
        )
    ```
  - In both `connect()` return sites (memory branch ~253, gmail branch ~270), pass `dedupe_store=dedupe_store, blob_store=blob_store` (both already in scope from lines 220-221).

- [ ] **Step 10: Green + full suite + types.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_pipeline_logging.py tests/test_health.py tests/core/test_observability.py -q` → all pass.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q` → no regression.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` → clean.

- [ ] **Step 11: Commit.**
  ```
  feat(observability): structured logging + metrics seam + health probe

  - RunReport.counters(): dependency-free disposition metrics for scraping.
  - core.health()/HealthReport: probe cursor/dedupe/blob reachability via
    their ports; dedupe probe claim+releases a reserved key (no state corruption).
  - logging.getLogger("mailflow.pipeline"): one body-free log line per
    disposition (only DecisionTrace fields — bodies are never logged).
  - Pipeline.health() + Mailflow.health() convenience surfaces.

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

---

## Task 3 — Make dead security config real or delete it

`SecurityConfig` (`config/schema.py:21-23`) has two never-referenced fields. **Decision (see CONTRACT DECISION below):**
- **`verify_scope_on_startup` → MAKE REAL.** Verify the granted Gmail OAuth scope at startup and fail fast (`AuthError`) if `gmail.readonly` is missing. It pairs with the existing OAuth path in `adapters/gmail/live.py`.
- **`read_allowlist` → DELETE.** It is unreferenced; its `# fail-closed (spec §9.1)` / "empty = read nothing" doc semantics are **inverted from real behavior** (the default `[]` reads *everything*); and enforcement was already deferred to Plan 2 (`docs/qa-findings.md` S4; core-spine plan §Deferred). A forward-declared "fail-closed" field that silently allows-all is worse than absent. Sender/mailbox allowlisting is already provided **and tested** by `OnlySenderFilter`/`OnlyDomainFilter`/`WhitelistFilter`. Plan 2 reintroduces it where it is actually enforced.

**Files:**
- `src/mailflow/config/schema.py` — `SecurityConfig` at `21-23` (delete `read_allowlist:22`, keep `verify_scope_on_startup:23`).
- `src/mailflow/adapters/gmail/live.py` — `OAuthTokenProvider` at `37-87` (add `verify_scopes()`); `run_service` at `140-216` (add `verify_scope` flag + a `verify_oauth_scopes` wiring helper); `GMAIL_READONLY` lives in `adapters/gmail/config.py:8`; `gmail_cfg.scopes` defaults to `[GMAIL_READONLY]` (`config.py:31`).
- `src/mailflow/facade.py` — `connect()` at `196-275` (+ `verify_scope_on_startup` param defaulted from `SecurityConfig`); `_build_gmail_live` at `322-362` (thread the flag into `run_service`).
- `docs/qa-findings.md` — update S4 (line 53). `email-ingestion-toolkit-solution.md` (lines 881, 950) — remove `read_allowlist` from the example YAML/comment.
- `tests/test_security_config.py` — NEW.

- [ ] **Step 1: Failing test — `read_allowlist` is gone; `verify_scope_on_startup` stays.**
  Create `tests/test_security_config.py`:
  ```python
  """Task 3: read_allowlist is deleted (was unenforced, fake fail-closed); the
  verify_scope_on_startup knob remains and is real."""

  from __future__ import annotations

  from mailflow.config.schema import SecurityConfig


  def test_read_allowlist_field_removed() -> None:
      assert "read_allowlist" not in SecurityConfig.model_fields


  def test_verify_scope_on_startup_default_true() -> None:
      assert "verify_scope_on_startup" in SecurityConfig.model_fields
      assert SecurityConfig().verify_scope_on_startup is True
  ```
  Run `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_security_config.py -q` → `test_read_allowlist_field_removed` FAILS (field still present).

- [ ] **Step 2: Delete `read_allowlist` + fix docs.**
  - In `src/mailflow/config/schema.py`, remove line 22 so `SecurityConfig` is:
    ```python
    class SecurityConfig(BaseModel):
        verify_scope_on_startup: bool = True   # verify granted Gmail OAuth scope at startup
    ```
  - In `docs/qa-findings.md` S4 (line 53): mark it **Withdrawn (field deleted in Phase 1)** with a one-line note — the field's "fail-closed; empty = read nothing" semantics were inverted from real behavior and enforcement was deferred to Plan 2; allowlisting is covered by the sender/domain filters; Plan 2 reintroduces an enforced version.
  - In `email-ingestion-toolkit-solution.md`: delete the `read_allowlist: [...]` line (881) and the `# honors read_allowlist` clause (950) so no doc advertises a non-existent field.
  Run Step-1 test → green. Run the full suite to confirm nothing imported the field: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q`.

- [ ] **Step 3: Failing test — `OAuthTokenProvider.verify_scopes()` fails closed on a missing scope.**
  The `credentials=` / `request_factory=` seams let this run without google-auth installed. Append to `tests/test_security_config.py`:
  ```python
  import pytest

  from mailflow.adapters.gmail.config import GMAIL_READONLY
  from mailflow.adapters.gmail.live import OAuthTokenProvider
  from mailflow.core.errors import AuthError


  class _FakeCreds:
      """Stand-in for google.oauth2.credentials.Credentials with the granted-scope
      surface google-auth populates after refresh()."""

      def __init__(self, *, granted: str | None, valid: bool = False) -> None:
          self.token = "access-token"
          self.refresh_token = "rt"
          self.valid = valid
          self.granted_scopes = granted

      def refresh(self, request: object) -> None:
          self.valid = True


  def _provider(creds: _FakeCreds) -> OAuthTokenProvider:
      return OAuthTokenProvider(
          client_id="cid", client_secret="sec", refresh_token="rt",
          token_uri="https://oauth2.googleapis.com/token", scopes=[GMAIL_READONLY],
          credentials=creds, request_factory=lambda: object(),
      )


  def test_verify_scopes_passes_when_granted() -> None:
      _provider(_FakeCreds(granted=GMAIL_READONLY)).verify_scopes([GMAIL_READONLY])


  def test_verify_scopes_raises_when_missing() -> None:
      prov = _provider(_FakeCreds(granted="https://www.googleapis.com/auth/userinfo.email"))
      with pytest.raises(AuthError) as exc:
          prov.verify_scopes([GMAIL_READONLY])
      assert GMAIL_READONLY in str(exc.value)
  ```
  Run → FAILS (`AttributeError: ... has no attribute 'verify_scopes'`).

- [ ] **Step 4: Implement `OAuthTokenProvider.verify_scopes()`.**
  In `src/mailflow/adapters/gmail/live.py`, add `AuthError` to the imports (`from mailflow.core.errors import AuthError` — add a top-level import; it is vendor-free) and add a method to `OAuthTokenProvider` (after `get_token`, ~line 87):
  ```python
  def verify_scopes(self, required: list[str]) -> None:
      """A9/§9: fail fast if the granted OAuth scopes do not cover `required`.
      Forces a token refresh (so google-auth populates granted_scopes), then checks
      the granted set. Falls back to the requested scopes only when the provider does
      not report granted scopes (older google-auth); a missing required scope raises
      AuthError so a mis-scoped credential never silently under-delivers mail."""
      self.get_token()  # forces refresh when the token is not yet valid
      raw = getattr(self._creds, "granted_scopes", None)
      if raw is None:
          granted = set(getattr(self._creds, "scopes", None) or [])
      elif isinstance(raw, str):
          granted = set(raw.split())
      else:
          granted = set(raw)
      missing = [s for s in required if s not in granted]
      if missing:
          raise AuthError(
              f"OAuth token is missing required scope(s) {missing}; "
              f"granted={sorted(granted)}"
          )
  ```
  Run Step-3 tests → green. (mypy: `granted` is `set[str]`; annotate the local if strict needs it — `granted: set[str]`.)

- [ ] **Step 5: Failing test — `run_service` honors the `verify_scope` flag.**
  `run_service` builds its own `OAuthTokenProvider` (constructing real google-auth `Credentials`), so test the **wiring helper** with a spy rather than the full SDK path. Append:
  ```python
  from mailflow.adapters.gmail.live import verify_oauth_scopes


  class _SpyTokenProvider:
      def __init__(self) -> None:
          self.calls: list[list[str]] = []

      def verify_scopes(self, required: list[str]) -> None:
          self.calls.append(required)


  def test_verify_oauth_scopes_runs_when_enabled() -> None:
      spy = _SpyTokenProvider()
      verify_oauth_scopes(spy, [GMAIL_READONLY], enabled=True)
      assert spy.calls == [[GMAIL_READONLY]]


  def test_verify_oauth_scopes_skipped_when_disabled() -> None:
      spy = _SpyTokenProvider()
      verify_oauth_scopes(spy, [GMAIL_READONLY], enabled=False)
      assert spy.calls == []
  ```
  Run → FAILS (`ImportError: cannot import name 'verify_oauth_scopes'`).

- [ ] **Step 6: Implement `verify_oauth_scopes` + wire into `run_service`.**
  In `src/mailflow/adapters/gmail/live.py`:
  - Add a module-level helper (typed against a tiny structural shape so it stays SDK-free and testable):
    ```python
    class _ScopeVerifiable(Protocol):
        def verify_scopes(self, required: list[str]) -> None: ...


    def verify_oauth_scopes(
        token_provider: _ScopeVerifiable, required: list[str], *, enabled: bool
    ) -> None:
        """Run the startup scope check when enabled (SecurityConfig.verify_scope_on_startup)."""
        if enabled:
            token_provider.verify_scopes(required)
    ```
    (Add `Protocol` to the `typing` import at line 14.)
  - In `run_service`, add a `verify_scope: bool = True` keyword param (after `start_watch`, before `filters`). Immediately after the `token_provider = OAuthTokenProvider(...)` block (~line 168) and **before** `bootstrap_watches` runs, call:
    ```python
    verify_oauth_scopes(token_provider, gmail_cfg.scopes, enabled=verify_scope)
    ```
    This fails fast before any watch is registered or cursor seeded.
  Run Step-5 tests → green.

- [ ] **Step 7: Thread the flag through the facade (make the schema field load-bearing).**
  In `src/mailflow/facade.py`:
  - Import the schema default so the field is genuinely referenced (not a magic literal):
    `from mailflow.config.schema import SecurityConfig` and a module constant
    `_DEFAULT_VERIFY_SCOPE = SecurityConfig().verify_scope_on_startup`.
  - Add `verify_scope_on_startup: bool = _DEFAULT_VERIFY_SCOPE` to `connect()`'s signature (group it with the security-relevant kwargs).
  - Pass it into `_build_gmail_live(..., verify_scope_on_startup=verify_scope_on_startup)` (gmail branch ~259).
  - In `_build_gmail_live` (~322), add `verify_scope_on_startup: bool = True` param and forward it to `run_service(..., verify_scope=verify_scope_on_startup)` (~355).
  - Append a test asserting the field is the wired default:
    ```python
    import inspect

    from mailflow.facade import connect


    def test_connect_exposes_verify_scope_param_defaulted_from_schema() -> None:
        param = inspect.signature(connect).parameters["verify_scope_on_startup"]
        assert param.default is SecurityConfig().verify_scope_on_startup is True
    ```

- [ ] **Step 8: Green + full suite + types.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_security_config.py -q` → all pass.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q` → no regression (existing Gmail tests that build `OAuthTokenProvider`/`run_service` via seams are unaffected; the new `verify_scope` defaults True but `run_service`'s SDK path is never exercised in tests).
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` → clean.

- [ ] **Step 9: Commit.**
  ```
  feat(security): real verify_scope_on_startup; delete fake read_allowlist

  - OAuthTokenProvider.verify_scopes(): fail-fast AuthError if the granted
    Gmail OAuth scope is missing the required gmail.readonly; wired into
    run_service (verify_scope flag) before any watch is registered, and
    threaded through connect()/_build_gmail_live from SecurityConfig's default.
  - Delete SecurityConfig.read_allowlist: it was unreferenced, its "fail-closed
    / empty = read nothing" doc was inverted from real behavior (default []
    read everything), and enforcement was already deferred to Plan 2. Sender/
    domain allowlisting is covered by OnlySender/OnlyDomain/Whitelist filters.
    Docs (qa-findings S4, solution YAML) updated.

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

---

## CONTRACT DECISION(s)

1. **`read_allowlist` deleted from `SecurityConfig` (cross-plan).** The core-spine plan (Task 14) and `docs/qa-findings.md` S4 forward-declared this field with enforcement deferred to **Plan 2** provider adapters. This plan deletes it in Phase 1 because (a) it is referenced nowhere, (b) its `# fail-closed (spec §9.1)` / "empty = read nothing" semantics are inverted from actual behavior (default `[]` processes everything — fake security), and (c) honoring the documented fail-closed semantics now would break every existing default config. **Downstream impact:** Plan 2 must reintroduce `read_allowlist` *at the point it is enforced* (provider adapter / filter injection), with non-default-breaking semantics, rather than relying on the Phase-1 forward declaration. No current code/test depends on the field (verified by grep); the example in `email-ingestion-toolkit-solution.md` and qa-findings S4 are updated.

2. **`verify_scope_on_startup` is consumed only on the Gmail live path.** `build_from_config` raises `NotImplementedError` for live providers, so the flag cannot be enforced there. It is made real via `facade.connect()` → `_build_gmail_live` → `run_service(verify_scope=...)` → `OAuthTokenProvider.verify_scopes()`, with `connect()`'s default sourced from `SecurityConfig().verify_scope_on_startup` so the schema field is genuinely load-bearing. **Downstream impact:** when Plan 2 wires the Graph adapter, it should perform an equivalent granted-scope check and read the same `SecurityConfig` field.

3. **Port-conformance check lives in `Pipeline.__init__`, not the builder.** This is deliberate so it covers *all* construction paths (overrides=, connect(), the Gmail composition root, hand-built pipelines) rather than only `build_from_config`. The extractor seam is preserved: `MimeExtractor` is accepted as a concrete type even though it does not implement the `ContentExtractor.extract` port (it exposes `extract_bytes`). **Downstream impact:** any future port added to `core/ports.py` that the pipeline injects should get a matching `_assert_port(...)` line.

---

## Self-review notes (resolved inline above)

- **Ownership (CLAUDE.md):** `core/{observability,ports}.py` are core-foundation-engineer's; `core/pipeline.py`, `builder.py`, `facade.py`, `config/`, `registry.py` are pipeline-engineer's; `adapters/gmail/*` is adapters-engineer's; `tests/` golden/e2e is fixtures-golden-engineer's. This plan touches files across owners — when run under subagent mode the lead must **serialize** the tracks (Task 2's `observability.py` change lands before the `pipeline.py` log/health wiring that imports it; Task 3's `live.py` change is adapters-owned). Sequencing within each task already respects "commit B before A imports it."
- **`mypy --strict` gotchas:** `getattr(port, "__protocol_attrs__", frozenset())` is typed `frozenset[str]`; `_assert_port` takes `port: type`. `health()` and `verify_scopes()` annotate local sets (`set[str]`). The `_record` helper and `Pipeline.health()` import from `observability` using `health as _health` to avoid the method/function name clash. `caplog` test params use `# type: ignore[no-untyped-def]` consistent with the repo's existing test style.
- **Tree stays importable every commit:** Task 2 Step 1 (restore test) and Step 3 (`counters()`) precede the pipeline import of `health`/`HealthReport` (Step 8). Task 3 deletes `read_allowlist` (Step 2) before adding scope logic; no commit leaves a dangling reference.
- **No new deps / JSON-compatible fixtures:** logging is stdlib; metrics/health are pure pydantic+stdlib; the scope check uses the existing google-auth seams (`credentials=`/`request_factory=`) so tests need no SDK. No YAML fixtures are introduced.
- **PII:** logging emits only `DecisionTrace` scalar fields — `test_body_is_never_logged` asserts the body string never appears in `caplog.text`. Scrubbing remains deferred per scope.
- **Frozen contracts untouched:** DLQ `dead_lettered` counting (`add_dead_letter` owns it; `_record` only adds a log beside the existing `record()`), monotonic cursor, dedupe shape, identity, and A11 sync-only/version policy are all unchanged.
