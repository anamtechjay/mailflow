# Gmail Subscription Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Harden the already-built Gmail subscription-lifecycle path (watch-renewal driver, reconciliation-by-reseed, bounded safety-net sweep) by adding characterization/regression tests that exercise the real daemon-thread driver closures — not just the pure helpers — and by extracting the one untested driver-scheduling guard into a tested predicate.

**Architecture:** The blocking Pub/Sub consume loop runs on the main thread; maintenance (watch renewal + the safety-net sweep) runs on `IntervalScheduler` daemon threads (`src/mailflow/adapters/gmail/scheduler.py`, vendor-free). Gmail renews by re-calling `users.watch` via `GmailWatchManager.renew_watch`; the sweep diffs each mailbox from the stored `historyId` via `sweep_once`. The cursor contract is honored throughout: `CursorStore.commit_if_ahead` is strictly monotonic (rejects `order ≤ stored`) and advances on every terminal disposition, so an overlapping push + sweep is idempotent.

**Tech Stack:** Python 3.12, pydantic 2.13, pytest 9, mypy (strict). PyYAML is NOT installed (config loader falls back to JSON). Google SDKs are optional (`gmail` extra) and imported locally — the unit suite never imports them. Always use the venv interpreter: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python`.

> **Scope:** This plan is **Gmail-only by design.** The Outlook/Graph subscription-lifecycle fast-follow (renewal driver, `GraphLifecycleHandler` wiring, delta-replay backfill) is tracked separately in `docs/superpowers/plans/2026-06-30-outlook-subscription-lifecycle.md`. Do not add Outlook/Graph tasks here.

**Conventions (from CLAUDE.md):** TDD, one behavior per cycle. mypy is configured `packages = ["mailflow"]`, strict — it checks the importable `src/mailflow` tree only (the `tests/` directory is NOT type-checked, so test fakes may be duck-typed). Every commit must leave the tree importable + mypy --strict clean. Commit messages end with the `Co-Authored-By` trailer below. **Never `git push`.**

---

## Task A1 — Characterize the renewal driver firing through the real IntervalScheduler

**CHARACTERIZATION** — passes on first run. It proves the real `renew_watches` closure (the one `run_service` arms via `scheduler.every(...)`) re-watches every handle on a scheduler tick. If it fails, the bug is in `renew_watches`/`renew_watch` — fix the impl per superpowers:systematic-debugging, never weaken the test.

**Files:**
- `tests/test_gmail_reliability.py` — APPEND a test (and a `WatchHandle`/`renew_watches` import). The file already imports `IntervalScheduler` (`from mailflow.adapters.gmail.scheduler import IntervalScheduler`) and uses `threading` + a `threading.Event` pattern (see `test_interval_scheduler_fires_and_stops`).
- Read-only ground truth:
  - `src/mailflow/adapters/gmail/bootstrap.py:32-35` — `renew_watches(*, watch_manager: GmailWatchManager, handles: list[WatchHandle]) -> list[WatchHandle]` returns `[watch_manager.renew_watch(h) for h in handles]`.
  - `src/mailflow/adapters/gmail/watch.py:15-19` — `WatchHandle` is a frozen dataclass with fields `mailbox: str`, `history_id: str`, `expiration: str = ""`.
  - `src/mailflow/adapters/gmail/watch.py:36-37` — `GmailWatchManager.renew_watch(self, handle: WatchHandle) -> WatchHandle`.
  - `src/mailflow/adapters/gmail/scheduler.py:19-30` — `IntervalScheduler.every(self, seconds: float, fn: Callable[[], object], name: str = "task") -> None` and `.stop()`.

- [ ] **Step 1: Write the characterization test.** Add the imports (top of `tests/test_gmail_reliability.py`, alongside the existing imports) and append the test. The fake watch manager is duck-typed (tests are not mypy-checked); it records the mailbox and returns a *fresh* `WatchHandle` exactly as the real manager does. The `tick` closure mirrors `run_service`'s `lambda: renew_watches(watch_manager=watch_manager, handles=handles)`.

  Add to the import block:
  ```python
  from mailflow.adapters.gmail.bootstrap import renew_watches, sweep_once
  from mailflow.adapters.gmail.watch import WatchHandle
  ```
  (the file already imports `sweep_once` alone — replace that line with the combined import above.)

  Append at the end of the file:
  ```python
  # ---- A1: renewal driver fires the real renew_watches closure through the scheduler ----

  class _RecordingWatchManager:
      """Duck-typed stand-in for GmailWatchManager: records each renewed mailbox and
      returns a fresh WatchHandle (mirrors GmailWatchManager.renew_watch)."""
      def __init__(self) -> None:
          self.renewed: list[str] = []

      def renew_watch(self, handle: WatchHandle) -> WatchHandle:
          self.renewed.append(handle.mailbox)
          return WatchHandle(mailbox=handle.mailbox, history_id="999", expiration="renewed")


  def test_renewal_driver_fires_renew_for_each_handle_through_scheduler() -> None:
      fired = threading.Event()
      watch_manager = _RecordingWatchManager()
      handles = [
          WatchHandle(mailbox="a@x.com", history_id="1"),
          WatchHandle(mailbox="b@x.com", history_id="2"),
      ]

      def tick() -> None:
          # mirrors run_service's lambda: renew_watches(watch_manager=..., handles=handles)
          renew_watches(watch_manager=watch_manager, handles=handles)
          fired.set()

      sched = IntervalScheduler()
      sched.every(0.01, tick, "gmail-watch-renew")
      try:
          assert fired.wait(2.0) is True                      # the daemon tick ran
      finally:
          sched.stop()
      assert watch_manager.renewed == ["a@x.com", "b@x.com"]  # both mailboxes re-watched on a tick
  ```

- [ ] **Step 2: Run it (expected: PASS immediately — characterization).**
  ```
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_gmail_reliability.py::test_renewal_driver_fires_renew_for_each_handle_through_scheduler -q
  ```
  Expected output: `1 passed`. If it does NOT pass, the bug is in `renew_watches`/`renew_watch` — investigate the impl, do not change the test.

- [ ] **Step 3: Confirm the test is wired (invert one assertion, watch it fail, then restore).** Temporarily change the final assertion to `assert watch_manager.renewed == []` and re-run the command from Step 2. Expected: it FAILS with `AssertionError` showing `['a@x.com', 'b@x.com'] == []`. This proves the daemon thread actually executed the closure (not a false green from `fired.wait` timing out). **Restore the assertion to `== ["a@x.com", "b@x.com"]`** and re-run — back to `1 passed`.

- [ ] **Step 4: Full suite + mypy.**
  ```
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy
  ```
  Expected: all tests pass; `Success: no issues found in N source files` (the test change is not mypy-checked, so the count is unchanged).

- [ ] **Step 5: Commit.**
  ```
  test(gmail): characterize watch-renewal driver firing through IntervalScheduler

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

---

## Task A2 — Extract + test the renewal-driver scheduling guard (red → green)

**GENUINE RED → GREEN.** The driver-registration guard in `run_service` has three conditions (`start_watch`, `watch_renew_seconds > 0`, non-empty `handles`) with no test. Extract it into a pure predicate, test the truth table, then replace the inline guard so the wiring decision is a single tested function.

**Files:**
- `src/mailflow/adapters/gmail/bootstrap.py` — ADD `should_schedule_renew` (same module as `renew_watches`; already imports `WatchHandle`).
- `src/mailflow/adapters/gmail/live.py:252` — the REAL current inline guard:
  ```python
      if start_watch and gmail_cfg.watch_renew_seconds > 0 and handles:
  ```
  (Inside `run_service`, which begins at `live.py:193`. The bootstrap import to extend is at `live.py:16`: `from mailflow.adapters.gmail.bootstrap import bootstrap_watches, renew_watches, sweep_once`.)
- `tests/test_gmail_reliability.py` — APPEND the truth-table test.
- Read-only: `src/mailflow/adapters/gmail/config.py:36` confirms the field name is `watch_renew_seconds: int = 86400`.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_gmail_reliability.py` (the `WatchHandle` import was added in Task A1; if A1 was skipped, add `from mailflow.adapters.gmail.bootstrap import should_schedule_renew` and `from mailflow.adapters.gmail.watch import WatchHandle`). Add the predicate to the bootstrap import line so it reads:
  ```python
  from mailflow.adapters.gmail.bootstrap import renew_watches, should_schedule_renew, sweep_once
  ```
  Append the test:
  ```python
  # ---- A2: renewal-driver scheduling guard truth table ----

  def test_should_schedule_renew_truth_table() -> None:
      H = [WatchHandle(mailbox="a@x.com", history_id="1")]
      # armed only when watch started AND interval positive AND at least one handle
      assert should_schedule_renew(start_watch=True, watch_renew_seconds=86400, handles=H) is True
      assert should_schedule_renew(start_watch=False, watch_renew_seconds=86400, handles=H) is False
      assert should_schedule_renew(start_watch=True, watch_renew_seconds=0, handles=H) is False
      assert should_schedule_renew(start_watch=True, watch_renew_seconds=86400, handles=[]) is False
  ```

- [ ] **Step 2: Run it (expected: FAIL — `ImportError: cannot import name 'should_schedule_renew'`).**
  ```
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_gmail_reliability.py::test_should_schedule_renew_truth_table -q
  ```
  Expected: a collection error / `ImportError` naming `should_schedule_renew`. This confirms the test fails for the stated reason (the helper does not exist yet).

- [ ] **Step 3: Add the predicate to `src/mailflow/adapters/gmail/bootstrap.py`.** Insert immediately after `renew_watches` (it ends at line 35), before `sweep_once`:
  ```python
  def should_schedule_renew(
      *, start_watch: bool, watch_renew_seconds: int, handles: list[WatchHandle]
  ) -> bool:
      """The watch-renewal daemon is armed only when the watch was started, the renew
      interval is positive, and at least one watch handle exists to renew. Extracted
      from run_service so the driver-scheduling decision is testable without running the
      blocking consume loop."""
      return bool(start_watch and watch_renew_seconds > 0 and handles)
  ```

- [ ] **Step 4: Run the test again (expected: PASS).**
  ```
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_gmail_reliability.py::test_should_schedule_renew_truth_table -q
  ```
  Expected: `1 passed`.

- [ ] **Step 5: Wire it into `live.py` (real wiring, no behavior change).** First extend the bootstrap import at `live.py:16` to:
  ```python
  from mailflow.adapters.gmail.bootstrap import (
      bootstrap_watches,
      renew_watches,
      should_schedule_renew,
      sweep_once,
  )
  ```
  Then replace the inline guard at `live.py:252`:
  ```python
      if start_watch and gmail_cfg.watch_renew_seconds > 0 and handles:
  ```
  with:
  ```python
      if should_schedule_renew(
          start_watch=start_watch,
          watch_renew_seconds=gmail_cfg.watch_renew_seconds,
          handles=handles,
      ):
  ```
  (The `scheduler.every(gmail_cfg.watch_renew_seconds, lambda: renew_watches(...), "gmail-watch-renew")` body below it is unchanged.)

- [ ] **Step 6: Full suite + mypy (the impl + live.py changes ARE mypy-checked).**
  ```
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy
  ```
  Expected: all tests pass; `Success: no issues found in N source files`. (`bool(... and handles)` is mypy-strict clean — the `bool(...)` wrapper collapses the `bool | list[WatchHandle]` short-circuit result.)

- [ ] **Step 7: Commit.**
  ```
  feat(gmail): extract+test renewal-driver scheduling guard (should_schedule_renew)

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

---

## Task A3 — Regression: overlapping sweep + push is idempotent and cursor-monotonic

**CHARACTERIZATION** — passes on first run. `tests/test_gmail_e2e.py::test_e2e_sweep_catches_mail` already proves the sweep emits. The missing proof: a sweep overlapping the push path does NOT double-emit and does NOT regress the cursor (the dedupe + strictly-monotonic-commit contract under overlap). If it double-emits or the cursor regresses, that is a real bug — investigate per superpowers:systematic-debugging, do not weaken the test.

**Files:**
- `tests/test_gmail_e2e.py` — APPEND a test. Reuse the EXISTING helpers verbatim: `MBX = "ops@acme.com"`, `STREAM`, `_b64url`, `_raw(mid, sender, subject)`, `_Resp(status, body)`, `_Msg(payload)`, `sweep_once` (already imported), and `_build(routes, *, start_cursor=100)` which returns the 6-tuple `(runtime, emit, dlq, cursor, client, transport)`.
- Read-only ground truth:
  - `src/mailflow/adapters/gmail/bootstrap.py:38-46` — `sweep_once(*, runtime, client, mailboxes)`: per mailbox it reads `client.get_profile(mailbox).get("historyId", "0")`, calls `runtime.provider.submit(mailbox, history_id)`, then `runtime.pipeline.run_once()`.
  - `src/mailflow/adapters/gmail/provider.py:54-89` — `fetch` diffs from the STORED cursor; the dedupe store (one `InMemoryDedupeStore` per `_build`) survives across both `run_once` calls.
  - `src/mailflow/stores/memory.py` — `InMemoryCursorStore.commit_if_ahead` rejects `order ≤ stored`.

- [ ] **Step 1: Write the characterization test.** Append to `tests/test_gmail_e2e.py`. Routes mirror `test_e2e_sweep_catches_mail` (`/profile` for the sweep's `get_profile`, `/messages/m9` for the raw, `/history` for the diff). The push and the sweep both resolve to historyId `200`, so the sweep's `commit_if_ahead(order=200)` is a no-op against the already-stored `200` — proving monotonicity under overlap.
  ```python
  # ---- reliability: a sweep overlapping the push path is idempotent + cursor-monotonic ----

  def test_e2e_sweep_overlapping_push_is_idempotent_and_monotonic() -> None:
      routes = [
          ("/profile", _Resp(200, {"emailAddress": MBX, "historyId": "200"})),
          ("/messages/m9", _Resp(200, {"raw": _b64url(_raw("m9", "c@p.com", "swept")),
                                       "sizeEstimate": 30})),
          ("/history", _Resp(200, {"history": [{"messagesAdded": [{"message": {"id": "m9"}}]}],
                                   "historyId": "200"})),
      ]
      runtime, emit, dlq, cursor, client, _ = _build(routes)        # start_cursor=100

      # 1) push delivers m9 -> emitted once, cursor advances 100 -> 200
      runtime.process_messages([_Msg({"emailAddress": MBX, "historyId": 200})])
      assert [e.email.subject for e in emit.events] == ["swept"]
      after_push = cursor.get("t", STREAM)
      assert after_push is not None and after_push.order == 200

      # 2) sweep overlaps the SAME mailbox/historyId: m9 is re-seen but already deduped
      sweep_once(runtime=runtime, client=client, mailboxes=[MBX])
      assert [e.email.subject for e in emit.events] == ["swept"]    # emitted EXACTLY once
      assert dlq.events == []
      after_sweep = cursor.get("t", STREAM)
      assert after_sweep is not None and after_sweep.order == 200   # never regressed
  ```

- [ ] **Step 2: Run it (expected: PASS immediately — characterization).**
  ```
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_gmail_e2e.py::test_e2e_sweep_overlapping_push_is_idempotent_and_monotonic -q
  ```
  Expected: `1 passed`. A double-emit or a cursor regression here is a real defect in the dedupe/cursor contract — investigate the impl, not the test.

- [ ] **Step 3: Confirm the test is wired (invert one assertion, watch it fail, then restore).** Temporarily change the post-sweep assertion to `assert [e.email.subject for e in emit.events] == ["swept", "swept"]` and re-run the Step 2 command. Expected: it FAILS, showing the actual list is `["swept"]` (one emit) — proving the sweep path actually executed and dedupe held. **Restore to `== ["swept"]`** and re-run — back to `1 passed`.

- [ ] **Step 4: Full suite + mypy.**
  ```
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q
  /Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy
  ```
  Expected: all tests pass; `Success: no issues found in N source files`.

- [ ] **Step 5: Commit.**
  ```
  test(gmail): regression — overlapping sweep+push is idempotent and cursor-monotonic

  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

---

## Self-Review

**Spec coverage (Gmail subscription-lifecycle hardening):**
- Renewal driver firing through the real daemon-thread scheduler — covered by A1 (characterization of the real `renew_watches` closure, not the prior `threading.Event.set` smoke test).
- Renewal-driver scheduling guard (`start_watch ∧ interval>0 ∧ handles`) — covered by A2 (extracted `should_schedule_renew`, 4-row truth table, wired into `live.py:252`).
- Sweep/push overlap idempotency + cursor monotonicity — covered by A3 (the dedupe + `commit_if_ahead` contract under overlap; the prior e2e test only proved the sweep emits).
- Out of scope by design: Outlook/Graph renewal driver, `GraphLifecycleHandler` wiring, delta-replay backfill — tracked in `docs/superpowers/plans/2026-06-30-outlook-subscription-lifecycle.md`.

**Placeholder scan:** No placeholders, no `...`/TODO/`_unused` artifacts. Every code block is complete and uses real helper/field names verified against current source (`renew_watches`, `should_schedule_renew`, `sweep_once`, `WatchHandle.{mailbox,history_id,expiration}`, `IntervalScheduler.every/stop`, `GmailConfig.watch_renew_seconds`, the `_build` 6-tuple, `_Resp`/`_Msg`/`_raw`/`_b64url`, `MBX`/`STREAM`).

**Type consistency:** Only A2 touches mypy-checked source (`bootstrap.py` + `live.py`); `should_schedule_renew` returns `bool` via `bool(...)` (collapses the `bool | list[WatchHandle]` short-circuit), and the `live.py` call passes the exact kwargs `start_watch`/`watch_renew_seconds`/`handles`. A1 and A3 touch only `tests/`, which `packages = ["mailflow"]` excludes from mypy — duck-typed fakes (`_RecordingWatchManager`) are intentional and verified safe at runtime. Every commit leaves the tree importable (the bootstrap helper is added and imported in the same A2 commit) and mypy-strict clean. No `git push`.
