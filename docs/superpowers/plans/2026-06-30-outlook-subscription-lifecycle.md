---
date: 2026-06-30
topic: Outlook (Microsoft Graph) subscription lifecycle, renewal driver, and bounded backfill — fast-follow wiring
status: ready
spec: docs/superpowers/specs/2026-06-29-mailflow-solution-doc-design.md
---

# Outlook (Microsoft Graph) Subscription Lifecycle — Fast-Follow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Make the Outlook/Microsoft-Graph subscription-lifecycle path real. Today the Graph seams exist but are **dead** (never instantiated): `live.py:run_service` builds the Graph runtime with `subscription_manager=None`, so (a) no renewal driver exists, (b) `lifecycle_handler` is `None` → dropped-subscription events are silently dropped, and (c) `GraphProvider.sweep` backfill is only reachable through the unwired lifecycle handler, and `SubscriptionReconciler.renew_all()` is never called. This plan first adds the **missing characterization tests** for those untested seams, then **WIRES them** (composition root + `live.py`) so the Graph fast-follow runs end-to-end: a renewal driver, dropped-subscription detection + reconciliation, and bounded missed-email backfill.

> **STATUS: deferred fast-follow (Outlook is out of V1 live scope).** Execute only when prioritizing the second provider. The Gmail subscription-lifecycle work is a **separate plan** — this plan contains ONLY the Graph/Outlook tasks.

**Architecture:** Two providers share one shape. The blocking consume loop runs on the main thread; maintenance (renewal + safety-net sweep) runs on `IntervalScheduler` daemon threads (`src/mailflow/adapters/gmail/scheduler.py`, vendor-free, reusable). Graph renews by `PATCH /subscriptions/{id}` with recreate-on-404 (`GraphSubscriptionManager.renew_watch`) driven across all tracked subscriptions by `SubscriptionReconciler.renew_all()`. Dropped-subscription recovery flows through `GraphLifecycleHandler` (`subscriptionRemoved → ensure_watch`, `reauthorizationRequired → renew_watch`, `missed → resync`), which is only constructed when a `subscription_manager` is injected into `build_graph_runtime`. Backfill is the cursor-bounded delta replay `GraphProvider.sweep(stream, cursor)` resuming from the stored `@odata.deltaLink`. The cursor contract is honored throughout: `CursorStore.commit_if_ahead` is strictly monotonic (rejects `order ≤ stored`) and advances on every terminal disposition, so overlapping push + sweep is idempotent via dedupe.

**Tech Stack:** Python 3.12, pydantic 2.13, pytest 9, mypy (strict). Vendor SDKs (azure-eventhub / msal / httpx) are imported **locally inside functions** in `live.py` only — the unit suite never imports them; tests use fakes. mypy runs `packages = ["mailflow"]` only (NOT `tests`), so test fakes need not satisfy production annotations. PyYAML is NOT installed (config loader falls back to JSON; keep YAML fixtures JSON-compatible).

Run tests/types with the venv interpreter only:
- `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest`
- `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`

Every commit must leave the tree importable and `mypy --strict` clean. Never `git push`. Every commit message ends with:
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

**Ownership note (CLAUDE.md):** This plan touches `src/mailflow/adapters/graph/{live.py,composition.py,config.py}` (wiring track) and `tests/providers/graph/` (fixtures track). The provider-internal `_order` hardening task (B9) touches `src/mailflow/adapters/graph/provider.py` (adapters-engineer track) — it is **flagged**, isolated as the final task, and coordinated with adapters-engineer, not edited silently. If executed by a single agent these are one coherent fast-follow; if split, the lead serializes the file-disjoint tracks.

**Keystone facts verified against the real code (feature/library, 2026-06-30):**
- `build_graph_runtime` (`composition.py:37-54`) still accepts an optional `subscription_manager: GraphSubscriptionManager | None = None` (and `lifecycle_handler: GraphLifecycleHandler | None = None`), and auto-builds the `lifecycle_handler` + `stream_resolver` when `subscription_manager` is present (`composition.py:61-90`). So **every existing Graph caller passing `None` keeps today's behavior** and the **Gmail path (`build_gmail_runtime`) is untouched**.
- `tests/providers/graph/` already contains one unrelated test (`test_classification_seam.py`) but **no `__init__.py`** — B1 still creates the package init.

> **CONTRACT DECISION (scheduler reuse):** `graph/live.py` imports the existing vendor-free `IntervalScheduler` from `adapters/gmail/scheduler.py` (a cross-adapter import) rather than forking it — it has no Gmail-specific imports (only `threading`/`typing`). Flagged for the lead: if no gmail→graph coupling is wanted, promote it to `src/mailflow/adapters/_scheduler.py` in a pre-task and update both imports; this plan's tests are unaffected.

---

## Part B — Graph/Outlook fast-follow wiring

> Test layout: graph adapter tests live under `tests/providers/graph/`. The directory exists and holds one unrelated file (`test_classification_seam.py`) but has **no `__init__.py`** on `feature/library`. Create `tests/providers/graph/__init__.py` once (Step in B1) so the package imports.

### Task B1 — Lifecycle handler reacts to all three signals (recovery policy) — CHARACTERIZATION

**Files:**
- `tests/providers/graph/__init__.py` (create, empty).
- `tests/providers/graph/test_lifecycle_recovery.py` (create; fixtures-golden-engineer).
- Read-only: `src/mailflow/adapters/graph/lifecycle.py:33-47` (`GraphLifecycleHandler.handle`), `src/mailflow/adapters/graph/notifications.py:33-39` (`GraphLifecycleEvent`).

- [ ] **Step 1: Create the package init.** `tests/providers/graph/__init__.py` (empty file).
- [ ] **Step 2: Write the characterization tests.** Fake subscription manager records `renew_watch` / `ensure_watch`; a resync callback records streams:
  ```python
  from mailflow.adapters.graph.lifecycle import GraphLifecycleHandler
  from mailflow.adapters.graph.notifications import GraphLifecycleEvent
  from mailflow.core.models import StreamRef

  S = StreamRef(mailbox="a@x.com", folder="inbox")

  class _FakeMgr:
      def __init__(self) -> None:
          self.renewed: list[object] = []
          self.ensured: list[StreamRef] = []
      def renew_watch(self, handle: object) -> object:
          self.renewed.append(handle); return handle
      def ensure_watch(self, stream: StreamRef) -> object:
          self.ensured.append(stream); return "new-sub"

  def test_reauthorization_required_renews() -> None:
      m = _FakeMgr()
      h = GraphLifecycleHandler(subscription_manager=m)
      ev = GraphLifecycleEvent(subscription_id="sub1", lifecycle_event="reauthorizationRequired")
      assert h.handle(ev) == "renewed" and m.renewed == ["sub1"]

  def test_subscription_removed_recreates_for_stream() -> None:
      m = _FakeMgr()
      h = GraphLifecycleHandler(subscription_manager=m)
      ev = GraphLifecycleEvent(subscription_id="sub1", lifecycle_event="subscriptionRemoved", stream=S)
      assert h.handle(ev) == "recreated" and m.ensured == [S]

  def test_subscription_removed_without_stream_is_ignored() -> None:
      m = _FakeMgr()
      h = GraphLifecycleHandler(subscription_manager=m)
      ev = GraphLifecycleEvent(subscription_id="sub1", lifecycle_event="subscriptionRemoved")
      assert h.handle(ev) == "ignored" and m.ensured == []

  def test_missed_triggers_resync_when_callback_present() -> None:
      m = _FakeMgr(); seen: list[StreamRef] = []
      h = GraphLifecycleHandler(subscription_manager=m, resync=seen.append)
      ev = GraphLifecycleEvent(subscription_id="sub1", lifecycle_event="missed", stream=S)
      assert h.handle(ev) == "resynced" and seen == [S]

  def test_missed_without_callback_is_ignored() -> None:
      h = GraphLifecycleHandler(subscription_manager=_FakeMgr())
      ev = GraphLifecycleEvent(subscription_id="sub1", lifecycle_event="missed", stream=S)
      assert h.handle(ev) == "ignored"
  ```
  Note: `GraphLifecycleHandler.__init__` is keyword-only (`*, subscription_manager, resync=None`); `subscription_manager` is typed against the structural `_SubManagerLike` Protocol, so the plain fake is accepted at runtime.
- [ ] **Step 3: Run (expected: PASS — characterizes the existing, untested `lifecycle.py`).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_lifecycle_recovery.py -q`
  **Confirm the suite is wired:** deliberately invert one assertion (e.g. change reauth's expected to `== "ignored"`), run, watch it FAIL, then **restore** it.
- [ ] **Step 4: mypy + commit.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
  Commit: `test(graph): characterize GraphLifecycleHandler recovery policy (renew/recreate/resync)`

### Task B2 — Subscription manager renew with recreate-on-404 (dropped-subscription detection) — CHARACTERIZATION

**Files:**
- `tests/providers/graph/test_subscription_manager.py` (create).
- Read-only: `src/mailflow/adapters/graph/subscriptions.py:58-82` (`ensure_watch`, `renew_watch`), `src/mailflow/adapters/graph/client.py:102-122` (`create_subscription`, `renew_subscription`), `src/mailflow/adapters/graph/transport.py:10-13` (`GraphError`).

- [ ] **Step 1: Write the characterization tests** with a fake `GraphClient` (duck-typed: only the methods used). Cover (a) renew PATCHes and returns the same id, (b) a 404 on renew recreates via `create_subscription` and remaps the handle, (c) a non-404 propagates:
  ```python
  from mailflow.adapters.graph.subscriptions import GraphSubscriptionManager, folder_resource
  from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
  from mailflow.adapters.graph.transport import GraphError
  from mailflow.core.models import StreamRef

  def _cfg() -> GraphConfig:
      return GraphConfig(tenant_id="t", client_id="c", client_secret_ref="r",
                         mailboxes=["a@x.com"], folders=["inbox"])
  def _evh() -> EventHubConfig:
      return EventHubConfig(namespace="ns", hub="h", tenant_domain="x.com")

  class _Client:
      def __init__(self, *, renew_error: GraphError | None = None) -> None:
          self.created: list[str] = []; self.renewed: list[str] = []
          self._renew_error = renew_error; self._n = 0
      def create_subscription(self, *, resource, notification_url, client_state, expiration_iso):
          self._n += 1; self.created.append(resource); return {"id": f"sub{self._n}"}
      def renew_subscription(self, subscription_id, expiration_iso):
          self.renewed.append(subscription_id)
          if self._renew_error is not None:
              raise self._renew_error
          return {"id": subscription_id}

  def test_ensure_then_renew_keeps_id() -> None:
      c = _Client(); mgr = GraphSubscriptionManager(client=c, config=_cfg(), eventhub=_evh(),
                                                    now_iso=lambda: "2026-06-30T00:00:00Z")
      h = mgr.ensure_watch(StreamRef(mailbox="a@x.com", folder="inbox"))
      assert mgr.renew_watch(h) == h and c.renewed == [str(h)]

  def test_renew_404_recreates_and_remaps() -> None:
      c = _Client(renew_error=GraphError(404, "gone"))
      mgr = GraphSubscriptionManager(client=c, config=_cfg(), eventhub=_evh(),
                                     now_iso=lambda: "2026-06-30T00:00:00Z")
      stream = StreamRef(mailbox="a@x.com", folder="inbox")
      h = mgr.ensure_watch(stream)                 # "sub1"
      new = mgr.renew_watch(h)                      # 404 -> recreate -> "sub2"
      assert new != h and mgr.stream_for_handle(new) == stream
      assert mgr.stream_for_handle(h) is None       # old id evicted

  def test_renew_non_404_propagates() -> None:
      c = _Client(renew_error=GraphError(500, "boom"))
      mgr = GraphSubscriptionManager(client=c, config=_cfg(), eventhub=_evh(),
                                     now_iso=lambda: "2026-06-30T00:00:00Z")
      h = mgr.ensure_watch(StreamRef(mailbox="a@x.com", folder="inbox"))
      try:
          mgr.renew_watch(h); assert False, "expected GraphError"
      except GraphError as exc:
          assert exc.status_code == 500
  ```
  (Verified: `GraphSubscriptionManager.__init__` is `*, client, config, eventhub, now_iso=_default_now_iso`; `renew_watch` returns the same `sub_id` on success, and on `GraphError.status_code == 404` recreates via `ensure_watch` and pops the old id; `GraphError(status_code, message)` carries `.status_code`.)
- [ ] **Step 2: Run (expected: PASS — characterizes existing untested `subscriptions.py`).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_subscription_manager.py -q`
  Confirm the suite is wired by inverting one assertion (e.g. `new == h`), watching it FAIL, then restore.
- [ ] **Step 3: mypy + commit.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
  Commit: `test(graph): characterize subscription renew + recreate-on-404`

### Task B3 — Reconciler tracks existing subscriptions and renews them all (the renewal target) — CHARACTERIZATION

**Files:**
- `tests/providers/graph/test_reconciler.py` (create).
- Read-only: `src/mailflow/adapters/graph/reconciler.py:27-56` (`desired_streams`, `reconcile`, `renew_all`).

`renew_all()` is the dead seam from the renewal feature — never called today. This is the object the renewal driver will tick.

- [ ] **Step 1: Write the characterization tests.** Fake client provides `list_subscriptions`; fake subscription manager records `register` / `ensure_watch` / `renew_watch`:
  ```python
  from mailflow.adapters.graph.reconciler import SubscriptionReconciler
  from mailflow.adapters.graph.subscriptions import folder_resource
  from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
  from mailflow.core.models import StreamRef

  def _cfg() -> GraphConfig:
      return GraphConfig(tenant_id="t", client_id="c", client_secret_ref="r",
                         mailboxes=["a@x.com"], folders=["inbox", "sentitems"])
  def _evh() -> EventHubConfig:
      return EventHubConfig(namespace="ns", hub="h", tenant_domain="x.com")

  class _Mgr:
      def __init__(self) -> None:
          self.registered: list[tuple[str, StreamRef]] = []
          self.ensured: list[StreamRef] = []; self.renewed: list[str] = []; self._n = 0
      def register(self, handle: object, stream: StreamRef) -> None:
          self.registered.append((str(handle), stream))
      def ensure_watch(self, stream: StreamRef) -> object:
          self._n += 1; self.ensured.append(stream); return f"new{self._n}"
      def renew_watch(self, handle: object) -> object:
          self.renewed.append(str(handle)); return str(handle)

  class _Client:
      def __init__(self, existing: list[dict]) -> None: self._existing = existing
      def list_subscriptions(self) -> list[dict]: return self._existing

  def test_reconcile_registers_existing_and_creates_missing() -> None:
      cfg, evh = _cfg(), _evh()
      inbox = folder_resource(StreamRef(mailbox="a@x.com", folder="inbox"))
      existing = [{"resource": inbox, "id": "sub-inbox", "notificationUrl": evh.notification_url},
                  {"resource": "users/other/...", "id": "x", "notificationUrl": "someone-else"}]
      mgr = _Mgr()
      rec = SubscriptionReconciler(client=_Client(existing), subscription_manager=mgr,
                                   config=cfg, eventhub=evh)
      handles = rec.reconcile()
      assert mgr.registered == [("sub-inbox", StreamRef(mailbox="a@x.com", folder="inbox"))]
      assert mgr.ensured == [StreamRef(mailbox="a@x.com", folder="sentitems")]  # missing one created
      assert handles[StreamRef(mailbox="a@x.com", folder="inbox").key] == "sub-inbox"

  def test_renew_all_renews_every_tracked_handle() -> None:
      cfg, evh = _cfg(), _evh()
      mgr = _Mgr()
      rec = SubscriptionReconciler(client=_Client([]), subscription_manager=mgr,
                                   config=cfg, eventhub=evh)
      rec.reconcile()                    # creates two
      rec.renew_all()
      assert sorted(mgr.renewed) == ["new1", "new2"]
  ```
  Note: `SubscriptionReconciler.__init__` is `*, client, subscription_manager, config, eventhub` and typed `subscription_manager: GraphSubscriptionManager`; the fake is structural. mypy does **not** run over `tests` (`packages = ["mailflow"]`), so the plain fake need not satisfy the annotation. Keep fakes plain. (`reconcile()` matches existing subs by our `eventhub.notification_url`, registers them, and creates the rest via `ensure_watch`; `renew_all()` iterates `self._handles` calling `renew_watch`.)
- [ ] **Step 2: Run (expected: PASS — characterizes existing untested `reconciler.py`).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_reconciler.py -q`
  Confirm the suite is wired by inverting one assertion (e.g. expect `mgr.ensured == []`), watching it FAIL, then restore.
- [ ] **Step 3: mypy + commit.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
  Commit: `test(graph): characterize reconciler tracking + renew_all (renewal-driver target)`

### Task B4 — Bounded backfill: `sweep` resumes from the stored deltaLink and is cursor-monotonic — CHARACTERIZATION

**Files:**
- `tests/providers/graph/test_backfill.py` (create).
- Read-only: `src/mailflow/adapters/graph/provider.py:41-115` (`request_sweep`, `fetch`, `sweep`), `src/mailflow/adapters/graph/client.py:75-95` (`delta_sweep`).

- [ ] **Step 1: Write the characterization tests** with a fake client whose `delta_sweep(user, folder, delta_link)` returns canned `(messages, new_delta)` and records the `delta_link` it was called with:
  ```python
  import json
  from mailflow.adapters.graph.provider import GraphProvider
  from mailflow.core.models import Cursor, StreamRef

  S = StreamRef(mailbox="a@x.com", folder="inbox")

  class _DeltaClient:
      def __init__(self, messages, new_delta) -> None:
          self._messages = messages; self._new_delta = new_delta
          self.delta_calls: list[str | None] = []
      def delta_sweep(self, user, folder, delta_link=None):
          self.delta_calls.append(delta_link)
          return self._messages, self._new_delta

  def test_sweep_resumes_from_stored_delta_link() -> None:
      c = _DeltaClient([{"id": "m1"}], "https://graph/delta?$deltatoken=NEXT")
      p = GraphProvider(client=c)
      stored = Cursor(value="https://graph/delta?$deltatoken=PREV", order=5)
      out = list(p.sweep(S, stored))
      assert c.delta_calls == ["https://graph/delta?$deltatoken=PREV"]   # resumed, not full
      assert len(out) == 1
      assert out[0].cursor.value == "https://graph/delta?$deltatoken=NEXT"  # new resume token

  def test_sweep_without_delta_link_does_full_resync() -> None:
      c = _DeltaClient([{"id": "m1"}], "https://graph/delta?$deltatoken=NEXT")
      p = GraphProvider(client=c)
      synthetic = Cursor(value="a@x.com:inbox#3", order=3)   # not a URL -> no token
      list(p.sweep(S, synthetic))
      assert c.delta_calls == [None]                          # full resync (safe)

  def test_sweep_cursor_order_is_strictly_increasing() -> None:
      c = _DeltaClient([{"id": "m1"}, {"id": "m2"}], "https://graph/delta?$deltatoken=N")
      p = GraphProvider(client=c)
      out = list(p.sweep(S, None))
      orders = [m.cursor.order for m in out]
      assert orders == sorted(orders) and len(set(orders)) == len(orders)  # monotone, unique

  def test_request_sweep_then_fetch_drains_once() -> None:
      c = _DeltaClient([{"id": "m1"}], "https://graph/delta?$deltatoken=N")
      p = GraphProvider(client=c)
      p.request_sweep(S)
      first = list(p.fetch(S, Cursor(value="https://graph/delta?$deltatoken=P", order=1)))
      second = list(p.fetch(S, Cursor(value="https://graph/delta?$deltatoken=P", order=1)))
      assert len(first) == 1 and second == []   # sweep stream drained after first fetch
  ```
  (Verified: `sweep` treats `cursor.value` as a resume token only when it `startswith("http")`, else passes `None` for a full resync; each yielded `RawMessage.cursor = Cursor(value=new_delta, order=self._order)` with `_order` incremented per message; `request_sweep` queues the stream and `fetch` drains it via `yield from self.sweep(...)` then removes it.)
- [ ] **Step 2: Run (expected: PASS — characterizes existing untested `sweep`/`request_sweep`).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_backfill.py -q`
  Confirm the suite is wired by inverting one assertion (e.g. expect `c.delta_calls == [None]` in the resume test), watching it FAIL, then restore.
- [ ] **Step 3: mypy + commit.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
  Commit: `test(graph): characterize cursor-bounded delta backfill (sweep resumes from deltaLink)`

### Task B5 — Runtime routes lifecycle events to the handler (recovery wiring at the consume seam) — CHARACTERIZATION

**Files:**
- `tests/providers/graph/test_runtime_lifecycle.py` (create).
- Read-only: `src/mailflow/adapters/graph/runtime.py:74-96` (`process_batch`), `src/mailflow/adapters/graph/composition.py:80-97`.

`GraphEventHubsRuntime.process_batch` only dispatches lifecycle events when `self.lifecycle_handler is not None`. Today it is always `None`. This test proves the route works once a handler is present (built by composition when a `subscription_manager` is supplied — Task B7 wires that).

- [ ] **Step 1: Write the characterization tests.** Construct the runtime directly with a recording lifecycle handler and fake provider/pipeline; feed an Event Hub event whose body is a `subscriptionRemoved` lifecycle payload; assert the handler was invoked and the event was checkpointed:
  ```python
  import json
  from mailflow.adapters.graph.runtime import GraphEventHubsRuntime
  from mailflow.adapters.graph.notifications import GraphLifecycleEvent

  class _Event:
      def __init__(self, body: str) -> None:
          self._b = body
      def body_as_str(self) -> str:
          return self._b
  class _Ckpt:
      def __init__(self) -> None: self.updated = 0
      def update(self, event) -> None: self.updated += 1
  class _Provider:
      def submit(self, note) -> None: raise AssertionError("no message submit on lifecycle")
  class _Pipeline:
      def run_once(self): raise AssertionError("no pipeline run on a pure lifecycle event")
  class _Handler:
      def __init__(self) -> None: self.seen: list[str] = []
      def handle(self, ev: GraphLifecycleEvent) -> str:
          self.seen.append(ev.lifecycle_event); return "recreated"

  def test_runtime_dispatches_lifecycle_to_handler_and_checkpoints() -> None:
      body = json.dumps({"value": [{
          "subscriptionId": "sub1", "clientState": "mailflow",
          "lifecycleEvent": "subscriptionRemoved",
          "resource": "users/a@x.com/mailFolders('inbox')/messages"}]})
      h = _Handler(); ck = _Ckpt()
      rt = GraphEventHubsRuntime(provider=_Provider(), pipeline=_Pipeline(),
                                 checkpointer=ck, client_state="mailflow", lifecycle_handler=h)
      rt.process_batch([_Event(body)])
      assert h.seen == ["subscriptionRemoved"] and ck.updated == 1

  def test_runtime_without_handler_still_checkpoints_lifecycle() -> None:
      body = json.dumps({"value": [{
          "subscriptionId": "sub1", "clientState": "mailflow",
          "lifecycleEvent": "missed",
          "resource": "users/a@x.com/mailFolders('inbox')/messages"}]})
      ck = _Ckpt()
      rt = GraphEventHubsRuntime(provider=_Provider(), pipeline=_Pipeline(),
                                 checkpointer=ck, client_state="mailflow", lifecycle_handler=None)
      rt.process_batch([_Event(body)])   # current dead-seam behavior: dropped but acked
      assert ck.updated == 1
  ```
  (Verified: `GraphEventHubsRuntime.__init__` is keyword-only `*, provider, pipeline, checkpointer, client_state, lifecycle_handler=None`; `process_batch(events, *, checkpointer=None)` parses notifications first — the lifecycle-shaped resource yields no message notes — then, only if `lifecycle_handler is not None`, parses+dispatches lifecycle events; it checkpoints every event afterward via `sink.update(event)`.)
- [ ] **Step 2: Run (expected: PASS — characterizes the runtime branch; the second test documents the dead-seam drop).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_runtime_lifecycle.py -q`
  Confirm the suite is wired by inverting one assertion (e.g. expect `h.seen == []`), watching it FAIL, then restore.
- [ ] **Step 3: mypy + commit.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
  Commit: `test(graph): runtime routes lifecycle events to handler + checkpoints`

### Task B6 — Add Graph renewal/sweep config fields (mirror Gmail) — RED→GREEN

**Files:**
- `src/mailflow/adapters/graph/config.py:38-39` (add fields to `GraphConfig` after `subscription_minutes`; wiring track).
- `tests/providers/graph/test_config_defaults.py` (create) — assert defaults.

`GraphConfig` has `subscription_minutes` (the requested expiry) but no driver cadence. Add `subscription_renew_seconds` and `sweep_seconds`, mirroring `GmailConfig.watch_renew_seconds` (86400) / `sweep_seconds` (900), so `live.py` can arm the daemons. `0` disables a feature (same convention).

- [ ] **Step 1: Write the failing test.**
  ```python
  from mailflow.adapters.graph.config import GraphConfig
  def test_graph_driver_cadence_defaults() -> None:
      c = GraphConfig(tenant_id="t", client_id="c", client_secret_ref="r", mailboxes=["a@x.com"])
      assert c.subscription_renew_seconds > 0       # renew well under subscription_minutes
      assert c.subscription_renew_seconds < c.subscription_minutes * 60
      assert c.sweep_seconds > 0
  ```
- [ ] **Step 2: Run (expected: FAIL — `AttributeError: 'GraphConfig' object has no attribute 'subscription_renew_seconds'`).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_config_defaults.py -q`
- [ ] **Step 3: Minimal impl.** In `src/mailflow/adapters/graph/config.py`, add to `GraphConfig` immediately after `subscription_minutes: int = 8640` (line 38), before `max_attempts`:
  ```python
      # reliability drivers (mirror Gmail). 0 disables a feature.
      subscription_renew_seconds: int = 43200   # renew every 12h (well under ~6-day expiry)
      sweep_seconds: int = 900                   # safety-net delta sweep every 15 min
  ```
- [ ] **Step 4: Green + mypy + commit.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_config_defaults.py -q`
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
  Commit: `feat(graph): add subscription_renew_seconds + sweep_seconds driver cadence to GraphConfig`

### Task B7 — Composition: build the subscription manager + reconciler and inject them (the keystone wiring) — RED→GREEN

**Files:**
- `src/mailflow/adapters/graph/composition.py` (add `build_graph_service`; wiring track).
- `tests/providers/graph/test_service_wiring.py` (create).
- Read-only: `src/mailflow/adapters/graph/composition.py:37-97` (`build_graph_runtime`), `src/mailflow/adapters/graph/reconciler.py`.

This is the contract keystone. `build_graph_runtime` already accepts `subscription_manager` (optional) and auto-builds the `lifecycle_handler` + `stream_resolver` when it is present — so the **Gmail path is untouched** (different composition root) and existing Graph callers that pass `None` keep today's behavior. The new `build_graph_service` is the supplier: it builds one service-level `GraphClient`, a `GraphSubscriptionManager`, a `SubscriptionReconciler`, calls `reconcile()` to create/track subscriptions, then calls `build_graph_runtime(subscription_manager=manager, ...)`. It returns `(runtime, reconciler)` so `live.py` can both run the consume loop and tick `reconciler.renew_all()` on the scheduler. It imports NO vendor SDK (stays unit-testable with fakes), mirroring `build_graph_runtime`.

- [ ] **Step 1: Write the failing test.** Inject fakes for token/transport/stores and emitters; drive the real `GraphClient` through a fake transport (no real HTTP). Assert: a manager is created, `reconcile()` ran (subscriptions tracked), the returned runtime has a non-`None` `lifecycle_handler`, the provider's `_stream_resolver` is wired, and the returned reconciler renews on `renew_all()`:
  ```python
  import json
  from mailflow.adapters.graph.composition import build_graph_service
  from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
  from mailflow.stores.memory import (
      InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore,
  )
  from mailflow.emit.memory import InMemoryEmitter   # adjust import to the real in-memory emitter

  class _Token:
      def get_token(self) -> str: return "tok"

  class _Resp:
      def __init__(self, status_code: int, payload: object) -> None:
          self.status_code = status_code; self._payload = payload
      def json(self) -> object: return self._payload
      @property
      def headers(self) -> dict[str, str]: return {}
      @property
      def content(self) -> bytes: return b""

  class _FakeTransport:
      """Route GET /subscriptions -> {"value": []} (so reconcile creates all desired
      streams) and POST /subscriptions -> {"id": "subN"}."""
      def __init__(self) -> None: self._n = 0
      def request(self, method, url, *, headers, json):
          if method == "GET" and url.endswith("/subscriptions"):
              return _Resp(200, {"value": []})
          if method == "POST" and url.endswith("/subscriptions"):
              self._n += 1; return _Resp(201, {"id": f"sub{self._n}"})
          return _Resp(200, {})

  def test_build_graph_service_wires_lifecycle_and_reconciler() -> None:
      cfg = GraphConfig(tenant_id="t", client_id="c", client_secret_ref="r",
                        mailboxes=["a@x.com"], folders=["inbox"])
      evh = EventHubConfig(namespace="ns", hub="h", tenant_domain="x.com")
      runtime, reconciler = build_graph_service(
          graph_cfg=cfg, eventhub=evh, tenant="t",
          token_provider=_Token(), transport=_FakeTransport(),
          emitter=InMemoryEmitter(), dlq_emitter=InMemoryEmitter(),
          cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
          blob_store=InMemoryBlobStore(),
      )
      assert runtime.lifecycle_handler is not None        # dropped-sub recovery now live
      assert runtime.provider._stream_resolver is not None  # folder resolution now live
      reconciler.renew_all()                              # the renewal-driver target works
  ```
  (Confirm the exact in-memory store/emitter import paths and constructors against `tests/test_gmail_e2e.py` / `src/mailflow/stores/memory.py` / `src/mailflow/emit/`; model the transport on that file's `_Transport`/`_Resp` fakes adapted to the Graph transport Protocol `request(method, url, *, headers, json) -> HttpResponse`.)
- [ ] **Step 2: Run (expected: FAIL — `ImportError: cannot import name 'build_graph_service' from 'mailflow.adapters.graph.composition'`).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_service_wiring.py -q`
- [ ] **Step 3: Minimal impl.** Add to `src/mailflow/adapters/graph/composition.py` (add the `SubscriptionReconciler` import at top; `GraphSubscriptionManager`, `GraphClient`, the store/port types, and `Filter` are already imported):
  ```python
  from mailflow.adapters.graph.reconciler import SubscriptionReconciler

  def build_graph_service(
      *,
      graph_cfg: GraphConfig,
      eventhub: EventHubConfig,
      tenant: str,
      token_provider: TokenProvider,
      transport: HttpTransport,
      emitter: Emitter,
      dlq_emitter: Emitter,
      cursor_store: CursorStore,
      dedupe_store: DedupeStore,
      blob_store: BlobStore,
      filters: list[Filter] | None = None,
      classifier: Classifier | None = None,
      checkpointer: Checkpointer | None = None,
  ) -> tuple[GraphEventHubsRuntime, SubscriptionReconciler]:
      """Wire the full Graph service: a service-level client + subscription manager,
      reconcile the desired subscription set (create missing / track existing), then
      build the runtime with the manager injected so lifecycle recovery + folder
      resolution are live. Returns the runtime and the reconciler the renewal driver
      ticks. Vendor-free (testable with fakes); live.py supplies the real SDK glue."""
      client = GraphClient(
          base_url=graph_cfg.base_url, token_provider=token_provider,
          transport=transport, max_retries=graph_cfg.max_attempts,
      )
      manager = GraphSubscriptionManager(client=client, config=graph_cfg, eventhub=eventhub)
      reconciler = SubscriptionReconciler(
          client=client, subscription_manager=manager, config=graph_cfg, eventhub=eventhub,
      )
      reconciler.reconcile()
      runtime = build_graph_runtime(
          graph_cfg=graph_cfg, eventhub=eventhub, tenant=tenant,
          token_provider=token_provider, transport=transport,
          emitter=emitter, dlq_emitter=dlq_emitter,
          cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
          filters=filters, classifier=classifier, checkpointer=checkpointer,
          subscription_manager=manager,
      )
      return runtime, reconciler
  ```
  Note: `build_graph_service` builds its own `GraphClient` for the manager/reconciler; `build_graph_runtime` independently builds a second `GraphClient` for the provider from the same `transport`/`token_provider` (today's shape — both are cheap, stateless wrappers). Do not refactor that in this task.
- [ ] **Step 4: Green + mypy + commit.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_service_wiring.py -q`
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
  Commit: `feat(graph): build_graph_service wires subscription manager + reconciler + lifecycle handler`

### Task B8 — live.py: run reconcile, inject the manager, arm the renewal + sweep daemons — RED→GREEN

**Files:**
- `src/mailflow/adapters/graph/live.py:173-221` (`run_service`; wiring track) + new `arm_graph_drivers` helper.
- `tests/providers/graph/test_live_driver.py` (create) — test the extracted, SDK-free driver-arming helper.
- Read-only: `src/mailflow/adapters/gmail/live.py:241-262` (the Gmail mirror), `src/mailflow/adapters/gmail/scheduler.py:14-30` (`IntervalScheduler.every`).

`run_service` itself blocks on `run_consume_loop` and pulls in azure SDKs, so it is not unit-testable. Extract the daemon-arming into a pure helper `arm_graph_drivers(scheduler, reconciler, runtime, graph_cfg)` (SDK-free) and test that. `run_service` then: calls `build_graph_service` (B7), arms drivers on an `IntervalScheduler`, runs the consume loop, and stops the scheduler in `finally` — exactly mirroring Gmail's `run_service`.

The **renewal driver** ticks `reconciler.renew_all()`. The **sweep driver** is the bounded-backfill safety net: per tick, for each desired stream, `runtime.provider.request_sweep(stream)` then `runtime.pipeline.run_once()` — `fetch` drains the sweep streams and `sweep` resumes from each stored deltaLink (cursor-bounded, idempotent; dedupe + monotonic cursor make overlap with push harmless).

- [ ] **Step 1: Write the failing test** for the extracted helper, with a fake scheduler that records `every(seconds, fn, name)` registrations and lets us invoke the registered closures:
  ```python
  from mailflow.adapters.graph.live import arm_graph_drivers   # to be added
  from mailflow.adapters.graph.config import GraphConfig

  class _FakeScheduler:
      def __init__(self) -> None: self.jobs: list[tuple[float, object, str]] = []
      def every(self, seconds, fn, name="task") -> None: self.jobs.append((seconds, fn, name))

  class _FakeReconciler:
      def __init__(self) -> None: self.renewed = 0
      def renew_all(self) -> None: self.renewed += 1
      def desired_streams(self):
          from mailflow.core.models import StreamRef
          return [StreamRef(mailbox="a@x.com", folder="inbox")]

  class _FakeProvider:
      def __init__(self) -> None: self.swept = []
      def request_sweep(self, s) -> None: self.swept.append(s)
  class _FakePipeline:
      def __init__(self) -> None: self.runs = 0
      def run_once(self): self.runs += 1
  class _FakeRuntime:
      def __init__(self) -> None: self.provider = _FakeProvider(); self.pipeline = _FakePipeline()

  def test_arm_graph_drivers_registers_renew_and_sweep() -> None:
      cfg = GraphConfig(tenant_id="t", client_id="c", client_secret_ref="r", mailboxes=["a@x.com"])
      sched, rec, rt = _FakeScheduler(), _FakeReconciler(), _FakeRuntime()
      arm_graph_drivers(scheduler=sched, reconciler=rec, runtime=rt, graph_cfg=cfg)
      names = {n for _, _, n in sched.jobs}
      assert names == {"graph-subscription-renew", "graph-sweep"}
      for _, fn, name in sched.jobs:   # invoke each registered closure once
          fn()
      assert rec.renewed == 1                          # renewal driver ticked
      assert rt.provider.swept and rt.pipeline.runs == 1  # bounded backfill ticked

  def test_arm_graph_drivers_respects_zero_disables() -> None:
      cfg = GraphConfig(tenant_id="t", client_id="c", client_secret_ref="r",
                        mailboxes=["a@x.com"], subscription_renew_seconds=0, sweep_seconds=0)
      sched, rec, rt = _FakeScheduler(), _FakeReconciler(), _FakeRuntime()
      arm_graph_drivers(scheduler=sched, reconciler=rec, runtime=rt, graph_cfg=cfg)
      assert sched.jobs == []                          # both disabled
  ```
- [ ] **Step 2: Run (expected: FAIL — `ImportError: cannot import name 'arm_graph_drivers' from 'mailflow.adapters.graph.live'`).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_live_driver.py -q`
- [ ] **Step 3: Minimal impl.** In `src/mailflow/adapters/graph/live.py`, add SDK-free imports at top (`Callable`/`Any`/`Protocol` are already on the `from typing import Any, Callable` line — add `Protocol`):
  ```python
  from typing import Any, Callable, Protocol

  from mailflow.adapters.gmail.scheduler import IntervalScheduler
  from mailflow.adapters.graph.composition import build_graph_runtime, build_graph_service
  ```
  Then add the helper (no azure import; reuses the Gmail `IntervalScheduler`):
  ```python
  class _SchedulerLike(Protocol):
      def every(self, seconds: float, fn: Callable[[], object], name: str = ...) -> None: ...

  class _ReconcilerLike(Protocol):
      def renew_all(self) -> None: ...
      def desired_streams(self) -> list[Any]: ...

  def arm_graph_drivers(
      *, scheduler: _SchedulerLike, reconciler: _ReconcilerLike,
      runtime: GraphEventHubsRuntime, graph_cfg: GraphConfig,
  ) -> None:
      """Arm the renewal + bounded-backfill daemons on `scheduler`, mirroring Gmail.
      0 disables a driver. The sweep closure queues a cursor-bounded delta replay per
      desired stream then runs the pipeline once (fetch drains the sweep + resumes from
      the stored deltaLink; dedupe + monotonic cursor make overlap with push harmless)."""
      if graph_cfg.subscription_renew_seconds > 0:
          scheduler.every(
              graph_cfg.subscription_renew_seconds,
              reconciler.renew_all,
              "graph-subscription-renew",
          )
      if graph_cfg.sweep_seconds > 0:
          def _sweep() -> None:
              for stream in reconciler.desired_streams():
                  runtime.provider.request_sweep(stream)
              runtime.pipeline.run_once()
          scheduler.every(graph_cfg.sweep_seconds, _sweep, "graph-sweep")
  ```
  Note: `runtime.pipeline` is typed `PipelineLike` (has `run_once`); `runtime.provider` is `GraphProvider` (has `request_sweep`). Both are public attributes set in `GraphEventHubsRuntime.__init__`, so `mypy --strict` resolves them. `reconciler.renew_all` as a bare method reference returns `None`, which is compatible with the `every` param `Callable[[], object]`; if strict objects, wrap as `lambda: reconciler.renew_all()`.
- [ ] **Step 4: Rewire `run_service`.** Replace the `runtime = build_graph_runtime(...)` + bare `run_consume_loop(...)` body (live.py:199-221) with the service build + driver arming + try/finally, mirroring Gmail:
  ```python
      runtime, reconciler = build_graph_service(
          graph_cfg=graph_cfg, eventhub=eventhub, tenant=tenant,
          token_provider=token_provider, transport=HttpxTransport(),
          emitter=emitter, dlq_emitter=dlq_emitter,
          cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
      )
      scheduler = IntervalScheduler()
      arm_graph_drivers(scheduler=scheduler, reconciler=reconciler,
                        runtime=runtime, graph_cfg=graph_cfg)
      try:
          run_consume_loop(
              runtime=runtime,
              fully_qualified_namespace=f"{eventhub.namespace}.servicebus.windows.net",
              hub=eventhub.hub, consumer_group=eventhub.consumer_group,
              connection_string=connection_string, credential=credential,
              checkpoint_blob_account_url=checkpoint_blob_account_url,
              checkpoint_connection_string=checkpoint_connection_string,
              checkpoint_container=checkpoint_container,
          )
      finally:
          scheduler.stop()
  ```
  (The existing top-level `from mailflow.adapters.graph.composition import build_graph_runtime` import at line 19 stays — Step 3's import line replaces it with the combined `build_graph_runtime, build_graph_service` form.)
- [ ] **Step 5: Green + mypy + import-without-extras guard.** Confirm `import mailflow.adapters.graph.live` still works WITHOUT the azure/msal extras (the new imports — `IntervalScheduler`, `build_graph_service`, `Protocol` — are all vendor-free):
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -c "import mailflow.adapters.graph.live"`
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_live_driver.py -q`
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
- [ ] **Step 6: Commit.** `feat(graph): wire subscription renewal + bounded-backfill drivers into run_service`

### Task B9 — Hardening (FLAGGED cross-track): make Graph sweep cursor survive process restart — RED→GREEN

**Files:**
- `src/mailflow/adapters/graph/provider.py:32,88-115` (`_order` init at line 32 + `sweep`; **adapters-engineer track — coordinate, do not edit silently**).
- `tests/providers/graph/test_backfill.py` (extend).

**Known limitation surfaced while reading the code:** `GraphProvider._order` starts at `0` each process (line 32) and `sweep` tags each yielded message `Cursor(value=new_delta, order=self._order)` (line 113). After a restart the first sweep emits `order=1,2,...`, but the stored cursor from before the restart has a larger `order`. `CursorStore.commit_if_ahead` is strictly monotonic (rejects `order ≤ stored`), so **the new deltaLink is rejected and never persisted** — the next sweep re-replays from the stale deltaLink. This is not a correctness bug (messages still flow; dedupe makes replay idempotent; backfill stays bounded by the *old* token), but it defeats deltaLink advancement across restarts and wastes a replay window. **Fix only if the lead/adapters-engineer agrees** (it touches the adapters track).

- [ ] **Step 1: Write the failing test** (append to `tests/providers/graph/test_backfill.py`). Assert that a sweep's emitted cursor order is seeded above a supplied baseline so `commit_if_ahead` accepts it post-restart:
  ```python
  def test_sweep_order_seeds_above_stored_cursor_for_restart_safety() -> None:
      c = _DeltaClient([{"id": "m1"}], "https://graph/delta?$deltatoken=N")
      p = GraphProvider(client=c)
      stored = Cursor(value="https://graph/delta?$deltatoken=P", order=500)  # pre-restart
      out = list(p.sweep(S, stored))
      assert out[0].cursor.order > 500   # would be accepted by a monotonic commit_if_ahead
  ```
- [ ] **Step 2: Run (expected: FAIL — current emitted order is `1`, not `> 500`).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/test_backfill.py::test_sweep_order_seeds_above_stored_cursor_for_restart_safety -q`
- [ ] **Step 3: Minimal impl.** In `GraphProvider.sweep`, after the `delta_link = (...)` assignment (ends at line 99) and before the `messages, new_delta = self.client.delta_sweep(...)` call (line 100), seed the counter from the stored cursor order:
  ```python
          if cursor is not None and cursor.order > self._order:
              self._order = cursor.order
  ```
  (This only ever raises `_order`, preserving in-process monotonicity for the push path too.)
- [ ] **Step 4: Green + mypy + commit.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/providers/graph/ -q`
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
  Commit: `fix(graph): seed sweep cursor order from stored cursor so deltaLink advances across restarts`

### Task B10 — Full-suite gate + import-without-extras guard — VERIFICATION

**Files:** none (verification only).

- [ ] **Step 1: Full suite.** `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q` — all green; record counts.
- [ ] **Step 2: mypy strict.** `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` — `Success: no issues`.
- [ ] **Step 3: Optional-extra guard.** Confirm both live modules import without vendor SDKs:
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -c "import mailflow.adapters.graph.live, mailflow.adapters.gmail.live; print('ok')"`
- [ ] **Step 4: Log QA.** Append any coverage observations to `docs/qa-findings.md` per CLAUDE.md (read it first; classify each against Phase scope — Graph is the deferred fast-follow, so these are *wiring closure*, not Phase gaps).

---

## Self-Review

- **Gmail path untouched by Graph wiring.** Verified against the real code: Gmail uses `build_gmail_runtime`; Graph uses the new `build_graph_service` → `build_graph_runtime`. The `subscription_manager` param on `build_graph_runtime` (`composition.py:52`) already defaults to `None`, so every existing Graph caller and all existing tests keep today's behavior. No shared module is mutated except `IntervalScheduler` is *imported* (not changed) by `graph/live.py`.
- **CONTRACT DECISION (scheduler reuse).** Chose to import the existing vendor-free `IntervalScheduler` from `adapters/gmail/scheduler.py` rather than fork a Graph copy — it has no Gmail-specific imports. Flagged for the lead; if no gmail→graph coupling is wanted, promote it to `src/mailflow/adapters/_scheduler.py` in a pre-task and update both imports (this plan's tests are unaffected).
- **Cursor contract honored.** Backfill cursors carry monotone, unique `order`s (B4) and resume from the stored `@odata.deltaLink` (bounded); B9 fixes the restart regression so `commit_if_ahead` accepts advancing deltaLinks. Overlap with the push path is idempotent via dedupe (B4's drain test).
- **TDD honesty.** Tasks over *already-built* behavior (B1–B5) are labeled **CHARACTERIZATION**: they pass on first run, so each includes an explicit "invert one assertion to confirm the suite is wired, then restore" discipline step. Tasks adding *new* code (B6, B7, B8, B9) are genuine **RED→GREEN** with an explicit expected failure message.
- **Ownership.** B9 is the only step touching the adapters-engineer track (`provider.py`); it is isolated as the final, optional, FLAGGED task so the rest of Part B (wiring track + tests) can land without cross-track coordination.
- **mypy strict.** Test-side fakes need not satisfy production annotations (mypy runs `packages=["mailflow"]` only, not `tests`). Production additions (`build_graph_service`, `arm_graph_drivers`, config fields, the sweep seed) use explicit types and public attributes already resolvable under strict.
- **Importability / bisectability.** B6 (config) lands before B7 (composition uses no new field but the driver does) and B7 before B8 (`live.py` imports `build_graph_service`); each commit leaves the tree importable and `mypy --strict` clean.
- **Line-number drift corrected vs the source combined plan.** `GraphProvider._order` is initialized at **line 32** (the source said 31); the B9 seed is inserted after the `delta_link = (...)` assignment ending at **line 99**. The Gmail mirror for B8 is `gmail/live.py:241-262` (the source said 184-215). `client.py` renew/create span is `102-122`. All other references verified accurate.
- **`tests/providers/graph/` is not empty.** It already holds an unrelated `test_classification_seam.py` (added by a later extract commit) but has **no `__init__.py`**; B1 still creates the package init.
