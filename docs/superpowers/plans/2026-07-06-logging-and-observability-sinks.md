# Logging Destinations & Observability Sinks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a consumer of `connect()` choose where mailflow's diagnostic logs go (a file / their own handler) AND pass their own function(s) that receive every per-email decision as structured data — so a "missing" email is always traceable.

**Architecture:** Two independent seams. (1) A **logging** seam: a `NullHandler` on the root `mailflow` logger (proper library default = silent) plus an opt-in `enable_logging(...)` convenience that attaches a `RotatingFileHandler`/`StreamHandler` — we reuse stdlib logging, never hand-roll file I/O. (2) An **observability sink** seam: optional `on_report`/`on_trace` callbacks bundled in an `Observers` object, threaded into `Pipeline`, invoked from the single `_record` funnel (per-trace) and at the end of `run_once` (per-run). Every callback invocation is wrapped so a buggy consumer callback can never break email ingestion. Defaults are all-off: zero behavior change when unused.

**Tech Stack:** Python 3.13, pydantic 2, stdlib `logging`, pytest, mypy --strict.

## Global Constraints

- **Backwards compatible / opt-in:** every new parameter defaults to `None`/off. With nothing passed, behavior and output are byte-identical to today. A `Pipeline` built without `observers` does no extra work.
- **Callbacks must never break ingestion:** every `on_report`/`on_trace` invocation is wrapped in `try/except Exception`; on error, log to the `mailflow.observability` logger at WARNING with `exc_info=True` and continue. A throwing callback must not change any disposition, cursor, or ack.
- **Library logging etiquette:** the library never calls `basicConfig()` and never attaches a real handler except through the explicit, consumer-invoked `enable_logging(...)`. The only always-on handler is a `NullHandler`. Per-module logger names (`mailflow.*`).
- **No secrets/bodies in callbacks by default:** callbacks receive `DecisionTrace`/`RunReport` (ids, dispositions, `matched_filter`, `reason`, counters) — never raw bodies or tokens.
- **mypy --strict clean; tree importable at every commit.** Callback types are precise (`Callable[[DecisionTrace], None]`, `Callable[[RunReport], None]`).
- **Git:** this repo is currently run **local-only** — do **not** `git push`. Treat each "Commit" step as an optional local checkpoint at the maintainer's discretion; commit trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File Structure

- Create `src/mailflow/logging_setup.py` — the `enable_logging(...)` convenience + `install_null_handler()`. (Named `logging_setup` not `logging` to avoid shadowing the stdlib module.)
- Modify `src/mailflow/__init__.py` — call `install_null_handler()` at import.
- Modify `src/mailflow/core/observability.py` — add `ReportObserver`/`TraceObserver` type aliases, the `Observers` dataclass, and the guarded `notify_observers(...)` helper. (Natural home: `RunReport`/`DecisionTrace` already live here; keeps `core` self-contained — no outward imports.)
- Modify `src/mailflow/core/pipeline.py` — `Pipeline.__init__` accepts `observers`; `_record` fires `on_trace`; `run_once` fires `on_report`.
- Modify `src/mailflow/facade.py` — `connect(...)` gains `log_file`, `log_level`, `on_report`, `on_trace`; wires `enable_logging` + builds `Observers`; passes it into the memory `Pipeline` and the live builders.
- Modify `src/mailflow/adapters/gmail/live.py`, `adapters/graph/composition.py`, `adapters/servicebus/composition.py` — thread `observers` through to the `Pipeline(...)` construction.
- Tests under `tests/` (paths per task).

**Ownership note (CONTRACT DECISION):** this feature deliberately edits `core/observability.py` and `core/pipeline.py` (core-owned). Justification: the `RunReport`/`DecisionTrace` already live in core, and the only place that sees *non-emitted* dispositions (drops/filters/duplicates/dead-letters — the whole point of the audit) is the pipeline. An emitter wrapper cannot capture them. This is the minimal, correct seam.

---

### Task 1: `enable_logging()` + NullHandler (the "give me a file" option)

**Files:**
- Create: `src/mailflow/logging_setup.py`
- Modify: `src/mailflow/__init__.py`
- Test: `tests/test_logging_setup.py`

**Interfaces:**
- Produces: `install_null_handler() -> None`; `enable_logging(*, level: str | int = "INFO", file: str | None = None, stream: "TextIO | None" = None, json: bool = False, max_bytes: int = 10_485_760, backup_count: int = 3) -> logging.Logger` — attaches ONE handler to the `mailflow` logger and returns it. `file` → `RotatingFileHandler`; else `stream` (default `sys.stderr`) → `StreamHandler`. `json=True` uses a compact JSON formatter. Idempotent: removes handlers it previously added (tagged with `_mailflow_managed = True`) before adding a new one.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_logging_setup.py
import json as _json
import logging
from pathlib import Path

from mailflow.logging_setup import enable_logging, install_null_handler


def test_null_handler_is_installed_on_root_mailflow_logger():
    install_null_handler()
    handlers = logging.getLogger("mailflow").handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)


def test_enable_logging_writes_to_file(tmp_path: Path):
    log_file = tmp_path / "mailflow.log"
    enable_logging(level="DEBUG", file=str(log_file))
    logging.getLogger("mailflow.pipeline").info("hello-from-test")
    for h in logging.getLogger("mailflow").handlers:
        h.flush()
    assert "hello-from-test" in log_file.read_text()


def test_enable_logging_json_format_is_parseable(tmp_path: Path):
    log_file = tmp_path / "mf.jsonl"
    enable_logging(level="INFO", file=str(log_file), json=True)
    logging.getLogger("mailflow.pipeline").warning("structured-line")
    for h in logging.getLogger("mailflow").handlers:
        h.flush()
    first = log_file.read_text().splitlines()[0]
    rec = _json.loads(first)
    assert rec["message"] == "structured-line"
    assert rec["level"] == "WARNING"


def test_enable_logging_is_idempotent_does_not_stack_handlers(tmp_path: Path):
    enable_logging(level="INFO", file=str(tmp_path / "a.log"))
    enable_logging(level="INFO", file=str(tmp_path / "b.log"))
    managed = [h for h in logging.getLogger("mailflow").handlers
               if getattr(h, "_mailflow_managed", False)]
    assert len(managed) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_logging_setup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.logging_setup'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/mailflow/logging_setup.py
"""Consumer-facing logging setup. The library ships SILENT (a NullHandler on the
root `mailflow` logger); the app opts in via enable_logging(). We never call
basicConfig and never attach a real handler except here — the app owns its logging."""

from __future__ import annotations

import json as _json
import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import TextIO

_ROOT = "mailflow"


def install_null_handler() -> None:
    """Attach a NullHandler once so `mailflow.*` loggers never emit
    'No handlers could be found' and the app stays in full control."""
    root = logging.getLogger(_ROOT)
    if not any(isinstance(h, logging.NullHandler) for h in root.handlers):
        root.addHandler(logging.NullHandler())


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return _json.dumps(payload)


def enable_logging(
    *,
    level: str | int = "INFO",
    file: str | None = None,
    stream: TextIO | None = None,
    json: bool = False,
    max_bytes: int = 10_485_760,
    backup_count: int = 3,
) -> logging.Logger:
    """Attach a single managed handler to the `mailflow` logger and return it.

    file  -> RotatingFileHandler(file); else stream (default sys.stderr) -> StreamHandler.
    json  -> compact JSON lines; else a readable text line.
    Idempotent: removes any handler this function added previously before adding one."""
    logger = logging.getLogger(_ROOT)
    logger.setLevel(level)
    for h in [h for h in logger.handlers if getattr(h, "_mailflow_managed", False)]:
        logger.removeHandler(h)

    handler: logging.Handler
    if file is not None:
        handler = RotatingFileHandler(
            file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler(stream or sys.stderr)

    handler.setLevel(level)
    handler.setFormatter(
        _JsonFormatter() if json
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    )
    setattr(handler, "_mailflow_managed", True)
    logger.addHandler(handler)
    return logger
```

```python
# src/mailflow/__init__.py  — add near the top, after existing imports
from mailflow.logging_setup import install_null_handler as _install_null_handler

_install_null_handler()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_logging_setup.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit (local checkpoint)**

```bash
git add src/mailflow/logging_setup.py src/mailflow/__init__.py tests/test_logging_setup.py
git commit -m "feat(logging): NullHandler default + opt-in enable_logging(file/stream/json)"
```

---

### Task 2: `Observers` + guarded `notify_observers` (the callback core)

**Files:**
- Modify: `src/mailflow/core/observability.py`
- Test: `tests/core/test_observers.py`

**Interfaces:**
- Consumes: existing `RunReport`, `DecisionTrace` (same module).
- Produces:
  - `ReportObserver = Callable[[RunReport], None]`, `TraceObserver = Callable[[DecisionTrace], None]`.
  - `@dataclass(frozen=True) class Observers: on_report: ReportObserver | None = None; on_trace: TraceObserver | None = None` with `def any(self) -> bool` returning whether either is set.
  - `notify_observers(cb, arg, *, logger) -> None` — calls `cb(arg)` inside `try/except Exception`; on error `logger.warning("observer callback %r raised", cb, exc_info=True)`; never re-raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_observers.py
import logging

from mailflow.core.models import Disposition
from mailflow.core.observability import (
    DecisionTrace, Observers, RunReport, notify_observers,
)

_log = logging.getLogger("mailflow.observability")


def _trace() -> DecisionTrace:
    return DecisionTrace(canonical_id="c1", tenant="t", stream="s",
                         disposition=Disposition.dropped, stage="filter",
                         matched_filter="blacklist", reason="spam.com")


def test_observers_any_reports_whether_a_callback_is_set():
    assert Observers().any() is False
    assert Observers(on_trace=lambda t: None).any() is True


def test_notify_delivers_the_argument():
    seen = []
    notify_observers(seen.append, _trace(), logger=_log)
    assert len(seen) == 1 and seen[0].matched_filter == "blacklist"


def test_notify_swallows_and_logs_a_throwing_callback(caplog):
    def boom(_): raise RuntimeError("consumer bug")
    with caplog.at_level(logging.WARNING, logger="mailflow.observability"):
        notify_observers(boom, _trace(), logger=_log)   # must NOT raise
    assert any("raised" in r.message or r.exc_info for r in caplog.records)


def test_notify_report_delivers_run_report():
    seen = []
    notify_observers(seen.append, RunReport(emitted=3), logger=_log)
    assert seen[0].emitted == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/core/test_observers.py -v`
Expected: FAIL with `ImportError: cannot import name 'Observers'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/mailflow/core/observability.py` (after `RunReport`; add `from dataclasses import dataclass` and `from typing import Callable` to the imports at the top):

```python
ReportObserver = Callable[["RunReport"], None]
TraceObserver = Callable[["DecisionTrace"], None]


@dataclass(frozen=True)
class Observers:
    """Optional consumer callbacks. on_trace fires once per message decision
    (emitted/dropped/duplicate/dead_lettered); on_report fires once per run_once()."""

    on_report: ReportObserver | None = None
    on_trace: TraceObserver | None = None

    def any(self) -> bool:
        return self.on_report is not None or self.on_trace is not None


def notify_observers(
    callback: "Callable[[object], None]", arg: object, *, logger: "logging.Logger"
) -> None:
    """Invoke a consumer callback defensively — a buggy callback must never break
    ingestion. Logs and swallows any exception; never re-raises."""
    try:
        callback(arg)
    except Exception:  # noqa: BLE001 - consumer code; isolate it from the pipeline
        logger.warning("observer callback %r raised", callback, exc_info=True)
```

Add `import logging` at the top of the module if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/core/test_observers.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit (local checkpoint)**

```bash
git add src/mailflow/core/observability.py tests/core/test_observers.py
git commit -m "feat(observability): Observers bundle + guarded notify_observers"
```

---

### Task 3: `Pipeline` invokes observers (surface the discarded RunReport)

**Files:**
- Modify: `src/mailflow/core/pipeline.py`
- Test: `tests/core/test_pipeline_observers.py`

**Interfaces:**
- Consumes: `Observers`, `notify_observers` (Task 2).
- Produces: `Pipeline.__init__(..., observers: Observers | None = None)`. `on_trace` fires from `_record` for every trace; `on_report` fires at the end of `run_once` before `return report`.

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_pipeline_observers.py
from mailflow import connect
from mailflow.core.models import Disposition, StreamRef
from mailflow.core.observability import Observers
from mailflow.providers.memory import SeedEmail

RAW = (b"From: a@partner.com\r\nTo: me@acme.com\r\n"
       b"Subject: hi\r\nMessage-ID: <m1@partner.com>\r\n\r\nbody\r\n")


def _seed():
    return {StreamRef(mailbox="me@acme.com", folder="inbox"): [SeedEmail("M1", RAW)]}


def test_on_trace_fires_once_per_message_via_connect():
    traces = []
    mf = connect("memory", seed=_seed(), on_trace=traces.append)
    mf.fetch_new()
    assert len(traces) == 1
    assert traces[0].disposition is Disposition.emitted
    assert traces[0].canonical_id


def test_on_report_fires_once_per_run_with_counters():
    reports = []
    mf = connect("memory", seed=_seed(), on_report=reports.append)
    mf.fetch_new()
    assert len(reports) == 1
    assert reports[0].emitted == 1


def test_throwing_callback_does_not_break_ingestion():
    def boom(_): raise RuntimeError("bug")
    got = []
    mf = connect("memory", seed=_seed(), on_trace=boom, on_email=got.append)
    mf.run()                       # must not raise
    assert len(got) == 1           # email still delivered
```

(Note: `connect(..., on_trace=/on_report=)` wiring lands in Task 4; this test also drives Task 3. Run the *unit* assertion below against `Pipeline` directly first if executing strictly task-by-task — otherwise Tasks 3+4 land together and this file passes at the end of Task 4. The direct-Pipeline check:)

```python
# tests/core/test_pipeline_observers.py  (add — exercises Task 3 in isolation)
from mailflow.core.observability import Observers as _Obs


def test_pipeline_accepts_observers_kwarg_directly():
    # Pipeline is constructed with observers= and does not raise; full behavioral
    # coverage comes from the connect() tests above once Task 4 wires it.
    assert "observers" in __import__(
        "inspect").signature(
        __import__("mailflow.core.pipeline", fromlist=["Pipeline"]).Pipeline.__init__
    ).parameters
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/core/test_pipeline_observers.py::test_pipeline_accepts_observers_kwarg_directly -v`
Expected: FAIL (`observers` not in signature)

- [ ] **Step 3: Write minimal implementation**

In `src/mailflow/core/pipeline.py`:

1. Import the helpers (top of file): `from mailflow.core.observability import DecisionTrace, DeadLetter, RunReport, Observers, notify_observers` (extend the existing observability import).

2. Add the constructor param (end of the `__init__` keyword list, after `dlq_store`):

```python
        observers: Observers | None = None,
```

3. Store it (after the existing assignments in `__init__`):

```python
        self._observers = observers or Observers()
```

4. Fire `on_trace` from the single funnel `_record` (append first, then notify):

```python
    def _record(self, report: RunReport, trace: DecisionTrace) -> None:
        report.record(trace)
        _log.info(
            "disposition=%s stage=%s canonical_id=%s stream=%s matched_filter=%s reason=%s",
            trace.disposition.value, trace.stage, trace.canonical_id, trace.stream,
            trace.matched_filter, trace.reason,
        )
        if self._observers.on_trace is not None:
            notify_observers(self._observers.on_trace, trace, logger=_log)
```

(Keep the existing `_log.info(...)` body exactly as-is; only add the trailing `if` block. Verify the real current body before editing — match it, don't replace it.)

5. Fire `on_report` at the end of `run_once`, immediately before `return report`:

```python
        if self._observers.on_report is not None:
            notify_observers(self._observers.on_report, report, logger=_log)
        return report
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/core/test_pipeline_observers.py::test_pipeline_accepts_observers_kwarg_directly -v`
Expected: PASS. (The `connect()`-based tests in this file pass after Task 4.)

- [ ] **Step 5: Commit (local checkpoint)**

```bash
git add src/mailflow/core/pipeline.py tests/core/test_pipeline_observers.py
git commit -m "feat(pipeline): fire on_trace per decision and on_report per run"
```

---

### Task 4: Wire `connect()` — `log_file`, `log_level`, `on_report`, `on_trace` (memory path end-to-end)

**Files:**
- Modify: `src/mailflow/facade.py`
- Test: `tests/core/test_pipeline_observers.py` (the `connect()` tests from Task 3 now pass), plus `tests/test_connect_logging.py`

**Interfaces:**
- Consumes: `enable_logging` (Task 1), `Observers` (Task 2), `Pipeline(observers=...)` (Task 3).
- Produces: `connect(..., log_file: str | None = None, log_level: str | int = "INFO", on_report: ReportObserver | None = None, on_trace: TraceObserver | None = None)`. When `log_file` is set (or `log_level` differs from default with intent), calls `enable_logging`. Builds `observers = Observers(on_report=on_report, on_trace=on_trace)` and passes it to every `Pipeline`/live builder.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connect_logging.py
import logging
from pathlib import Path

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

RAW = (b"From: a@x.com\r\nTo: me@acme.com\r\nSubject: s\r\n"
       b"Message-ID: <m@x.com>\r\n\r\nbody\r\n")


def _seed():
    return {StreamRef(mailbox="me@acme.com", folder="inbox"): [SeedEmail("M1", RAW)]}


def test_connect_log_file_writes_pipeline_logs(tmp_path: Path):
    log_file = tmp_path / "run.log"
    mf = connect("memory", seed=_seed(), log_file=str(log_file), log_level="INFO")
    mf.fetch_new()
    for h in logging.getLogger("mailflow").handlers:
        h.flush()
    text = log_file.read_text()
    assert "disposition=emitted" in text


def test_connect_on_trace_receives_each_decision():
    seen = []
    connect("memory", seed=_seed(), on_trace=seen.append).fetch_new()
    assert [t.disposition.value for t in seen] == ["emitted"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_connect_logging.py -v`
Expected: FAIL with `TypeError: connect() got an unexpected keyword argument 'log_file'`

- [ ] **Step 3: Write minimal implementation**

In `src/mailflow/facade.py`:

1. Imports (top): `from mailflow.core.observability import HealthReport, Observers, ReportObserver, TraceObserver, health as _health` (extend the existing line); `from mailflow.logging_setup import enable_logging`.

2. Add params to `connect(...)` (after `delivery=`):

```python
    log_file: str | None = None,
    log_level: str | int = "INFO",
    on_report: ReportObserver | None = None,
    on_trace: TraceObserver | None = None,
```

3. At the top of the body (right after `pol = normalize_attachment_policy(attachments)`):

```python
    if log_file is not None:
        enable_logging(level=log_level, file=log_file)
    observers = Observers(on_report=on_report, on_trace=on_trace)
```

4. Pass `observers=observers` to the memory `Pipeline(...)` (add the kwarg alongside `config=`):

```python
            config=PipelineConfig(tenant=tenant, on_filtered=on_filtered),
            observers=observers,
```

5. Thread `observers=observers` into each live builder call — add the kwarg to the `_build_gmail_live(...)`, `_build_graph_servicebus_live(...)`, and `_build_graph_live(...)` calls in `connect()`. (The builders gain the param in Task 5; add the call-site kwarg now so Task 5 is a pure signature/threading change.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_connect_logging.py tests/core/test_pipeline_observers.py -v`
Expected: PASS (all — including the `connect()` on_trace/on_report tests from Task 3).

- [ ] **Step 5: Commit (local checkpoint)**

```bash
git add src/mailflow/facade.py tests/test_connect_logging.py tests/core/test_pipeline_observers.py
git commit -m "feat(connect): log_file/log_level + on_report/on_trace sinks (memory path e2e)"
```

---

### Task 5: Thread `observers` into the live providers (gmail, graph, servicebus)

**Files:**
- Modify: `src/mailflow/facade.py` (`_build_gmail_live`, `_build_graph_live`, `_build_graph_servicebus_live` signatures + pass-through)
- Modify: `src/mailflow/adapters/gmail/live.py` (`run_service` → `Pipeline(observers=...)`)
- Modify: `src/mailflow/adapters/graph/composition.py` (`build_graph_runtime` → `Pipeline(observers=...)`)
- Modify: `src/mailflow/adapters/servicebus/composition.py` (`build_servicebus_graph_runtime` → `Pipeline(observers=...)`)
- Test: `tests/test_transport_observers.py`

**Interfaces:**
- Consumes: `Observers` (Task 2), the call-site kwargs added in Task 4.
- Produces: `observers: Observers | None = None` param on all three `_build_*_live` helpers, all three live `run_service`/composition builders, threaded to the `Pipeline(...)` construction inside each.

**Implementation notes for the executor:** each of the three adapters constructs the shared `Pipeline` (directly in gmail `run_service`, via `build_graph_runtime` for Event Hubs, via `build_servicebus_graph_runtime` for Service Bus). For each: (a) add `observers: Observers | None = None` to the function that builds the `Pipeline`; (b) add `observers=observers` to that `Pipeline(...)` call; (c) thread the param down from the `_build_*_live` facade helper. Locate each `Pipeline(` construction with `grep -n "Pipeline(" src/mailflow/adapters/**/*.py` before editing. Import `Observers` locally where the SDK imports already are (keep import discipline).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transport_observers.py
"""The Graph Event Hubs and Service Bus live runtimes must deliver on_trace to the
consumer, same as the memory path. Drives the real pipeline via fakes (no Azure SDK)."""
import json
from typing import Any

from mailflow.adapters.graph.composition import build_graph_runtime
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.core.observability import Observers
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import (
    InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore,
)

MBX = "ops@acme.com"


class _Tok:
    def get_token(self) -> str: return "t"


class _Resp:
    def __init__(self, code, payload): self.status_code, self._p, self.headers, self.content = code, payload, {}, b""
    def json(self): return self._p


class _Transport:
    def __init__(self, msg): self.msg = msg
    def request(self, method, url, *, headers, json):
        if "/attachments" in url: return _Resp(200, {"value": []})
        if "/messages/" in url: return _Resp(200, self.msg)
        return _Resp(200, {"value": []})


class _EHEvent:
    def __init__(self, body): self._b = body
    def body_as_str(self): return self._b


class _Ckpt:
    def update(self, e): ...


def _msg():
    return {"id": "MSG1", "internetMessageId": "<m@x>", "subject": "s",
            "from": {"emailAddress": {"address": "a@x.com"}},
            "toRecipients": [{"emailAddress": {"address": MBX}}], "ccRecipients": [],
            "body": {"contentType": "text", "content": "b"}, "bodyPreview": "b",
            "receivedDateTime": "2026-07-06T10:00:00Z", "sentDateTime": "2026-07-06T09:00:00Z",
            "isDraft": False, "hasAttachments": False, "parentFolderId": "inbox", "categories": []}


def _note():
    return json.dumps({"value": [{"subscriptionId": "s", "changeType": "created",
        "clientState": "mailflow", "resource": f"Users/{MBX}/Messages/MSG1",
        "resourceData": {"id": "MSG1"}}]})


def test_graph_eventhub_runtime_delivers_on_trace():
    traces = []
    rt = build_graph_runtime(
        graph_cfg=GraphConfig(tenant_id="t", client_id="c", client_secret_ref="env://X", mailboxes=[MBX]),
        eventhub=EventHubConfig(namespace="e", hub="h", tenant_domain="acme.com"),
        tenant="acme", token_provider=_Tok(), transport=_Transport(_msg()),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), observers=Observers(on_trace=traces.append),
    )
    rt.process_batch([_EHEvent(_note())], checkpointer=_Ckpt())
    assert [t.disposition.value for t in traces] == ["emitted"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_transport_observers.py -v`
Expected: FAIL with `TypeError: build_graph_runtime() got an unexpected keyword argument 'observers'`

- [ ] **Step 3: Write minimal implementation**

For each of the three builders, add the param and thread it to `Pipeline(...)`. Example for `build_graph_runtime` in `src/mailflow/adapters/graph/composition.py`:

```python
# add to the keyword-only params (near lifecycle_handler=None):
    observers: Observers | None = None,
# add the import at top (local/module import consistent with the file's style):
from mailflow.core.observability import Observers
# add to the Pipeline(...) construction:
        observers=observers,
```

Repeat the identical three-line change for `build_servicebus_graph_runtime` (`servicebus/composition.py`) and the gmail `run_service` `Pipeline(...)` (`gmail/live.py`). Then thread `observers` from the facade helpers: add `observers: Observers | None = None` to `_build_gmail_live`, `_build_graph_live`, `_build_graph_servicebus_live`, and pass `observers=observers` into the `run_service(...)` / composition call inside each `live()` closure.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_transport_observers.py -v`
Expected: PASS

- [ ] **Step 5: Commit (local checkpoint)**

```bash
git add src/mailflow/facade.py src/mailflow/adapters/gmail/live.py src/mailflow/adapters/graph/composition.py src/mailflow/adapters/servicebus/composition.py tests/test_transport_observers.py
git commit -m "feat(live): thread observers into gmail/graph/servicebus pipelines"
```

---

### Task 6: Docs — README recipes for both options

**Files:**
- Modify: `README.md`
- Modify: `docs/qa-findings.md` (append the review note for this feature)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add a "Logs & observability" section to README.md**

Add, near the existing `connect()` docs:

````markdown
## Logs & observability

mailflow ships **silent** (a `NullHandler`). Turn logs on where you want them:

```python
from mailflow import connect

# 1) Send diagnostic logs to a rotating file (or omit log_file for stderr):
mf = connect("gmail", credentials=creds, mailbox="me",
             log_file="mailflow.log", log_level="INFO")

# 2) Pass your own function(s) that receive every per-email decision as structured data:
def audit(trace):            # DecisionTrace: canonical_id, disposition, matched_filter, reason
    db.insert(trace.model_dump())

def metrics(report):         # RunReport: emitted / dropped / duplicates / dead_lettered
    statsd.gauge("mailflow.emitted", report.emitted)

mf = connect("gmail", credentials=creds, mailbox="me",
             on_trace=audit, on_report=metrics)
```

`on_trace` fires once per message (including **dropped/filtered/duplicate/dead-lettered** — so a
"missing" email is always explained by its `matched_filter` + `reason`). `on_report` fires once per
run with the counters. A callback that raises is logged and swallowed — it never interrupts
ingestion. Both default to off; combine them freely with `log_file`. For full control, ignore these
and attach your own handler to `logging.getLogger("mailflow")`.
````

- [ ] **Step 2: Append the QA-findings entry**

```markdown
## Review 2026-07-06 — Logging destinations & observability sinks

New feature (local): `enable_logging()` + NullHandler default; `connect(log_file=, log_level=,
on_report=, on_trace=)`; `Observers` threaded into Pipeline (fires from `_record` per trace and
`run_once` per report) and all three live providers. Callbacks are guarded (throwing callback
logged + swallowed, ingestion unaffected). Defaults off → backwards compatible.
**CONTRACT DECISION:** deliberate core edit (`observability.py`, `pipeline.py`) — non-emitted
dispositions are only visible in the pipeline, so an emitter wrapper cannot capture the audit.
```

- [ ] **Step 3: Commit (local checkpoint)**

```bash
git add README.md docs/qa-findings.md
git commit -m "docs: log_file + on_trace/on_report recipes; QA-findings entry"
```

---

## Self-Review

**Spec coverage:** Both requested options are covered — the **filename** (`log_file=`, Task 1+4, RotatingFileHandler-backed) and the **callback function** (`on_trace`/`on_report`, Tasks 2–5). Live providers (Task 5) reach parity with memory. Guardrail (throwing callback can't break ingestion) is specified and tested (Task 2 + Task 3). Docs (Task 6).

**Placeholder scan:** none — every step has concrete code/commands/expected output.

**Type consistency:** `Observers`, `ReportObserver`, `TraceObserver`, `notify_observers`, `enable_logging`, `install_null_handler` are named identically across Tasks 1–6; `Pipeline(observers=...)`, `build_graph_runtime(observers=...)`, `connect(on_trace=/on_report=)` match their definitions.

**Risk note:** Task 5 touches three adapter files with the identical three-line change; the executor must `grep -n "Pipeline("` to locate each construction site rather than assume line numbers.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-06-logging-and-observability-sinks.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session with checkpoints for review.

Which approach?
