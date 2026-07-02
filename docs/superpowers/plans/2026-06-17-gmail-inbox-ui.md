---
date: 2026-06-17
topic: Gmail-like inbox UI — implementation plan (phase by phase)
status: ready
spec: docs/superpowers/specs/2026-06-17-gmail-inbox-ui-design.md
---

# Gmail-like inbox UI — implementation plan

> **Conventions:** TDD for the store layer. Run `python -m pytest --import-mode=importlib`
> and `python -m mypy`. The UI/server is a self-contained script (`scripts/inbox_app.py`)
> using stdlib `http.server` + vanilla JS — no new dependencies. The Pub/Sub consumer
> (`run_gmail_live.py`) and the simple feed dashboard stay unchanged.

## Overview
A two-pane Gmail-style inbox: conversation list (left) + reading pane (right), with full
read/unread tracking, live unread badge, and downloadable attachments — backed by
`SqliteEmailStore`.

## Success Criteria
- [ ] `SqliteEmailStore` tracks read/unread: `thread_list()`, `thread(key)`, `mark_read(key)` (unit-tested).
- [ ] `scripts/inbox_app.py` serves the SPA + JSON API from the DB.
- [ ] Left list shows conversations; unread are bold + ● with a header unread count.
- [ ] Clicking a conversation opens it in the reading pane (messages 1→N) and marks it read.
- [ ] New email appears live (poll) with badge + tab-title update; attachments download.
- [ ] Full offline suite green; `mypy --strict` clean; existing scripts untouched.

## Current State
- `src/mailflow/persistence/sqlite_store.py` — `emails` table (+ `thread_key`), `save()`,
  `threads()`, `recent()`, `count()`.
- `scripts/showcase_dashboard.py` — current live feed (kept as-is).
- Attachments served from `attachments/` via `/file?ref=` (reuse pattern).

## What We're NOT Doing
Compose/reply/send, search, labels/folders, auth, websockets.

---

## Phase 1 — Store: read/unread + thread accessors (TDD)

- [ ] **Task 1.1: `read` column + migration**
  - Steps: add `read INTEGER DEFAULT 0` to `_SCHEMA`; add `read` to `_COLUMNS` + `save()` row (value `0`); add `PRAGMA table_info` migration to `ALTER TABLE emails ADD COLUMN read INTEGER DEFAULT 0` for old DBs.
  - Test: saving an event leaves it `read=0` (assert via a `thread_list` unread flag).
  - Check: `pytest tests/persistence -q`; `mypy`.

- [ ] **Task 1.2: `thread_list()` summaries with unread**
  - Steps: `SELECT thread_key, MAX(date_utc) last, COUNT(*) c, SUM(CASE WHEN read=0 THEN 1 ELSE 0 END) unread_msgs, ...` group by thread_key order by last desc; build `{thread_key, subject(first msg), last_from, last_date, count, unread(bool)}`; also return `unread_total` (count of threads with any unread).
  - Test: 1 root + 1 reply (same thread) + 1 other → 2 summaries; unread True initially; `unread_total == 2`.
  - Check: `pytest`; `mypy`.

- [ ] **Task 1.3: `thread(thread_key)` + `mark_read(thread_key)`**
  - Steps: `thread(key)` → messages ordered (reuse the per-thread query from `threads()`), include `read`; `mark_read(key)` → `UPDATE emails SET read=1 WHERE thread_key=?` (returns rows affected).
  - Test: `mark_read` flips unread → `thread_list` unread False + `unread_total` drops; `thread(key)` returns ordered messages.
  - Check: `pytest`; `mypy`.

## Phase 2 — Backend API (`scripts/inbox_app.py`)

- [ ] **Task 2.1: App skeleton + store wiring**
  - Steps: new `scripts/inbox_app.py` — `SqliteEmailStore(EMAIL_DB)`, a background Pub/Sub pull thread that `save()`s (reuse the consumer pattern from the dashboard so the inbox fills itself), `HTTPServer` on :5001.
  - Check: `python -m py_compile scripts/inbox_app.py`; starts and serves `/` (manual).

- [ ] **Task 2.2: JSON endpoints**
  - Steps: `GET /api/threads` → `{unread_total, threads:[…]}` from `thread_list()`;
    `GET /api/thread?key=` → `thread(key)`; `POST /api/thread/read?key=` → `mark_read` then `{unread_total}`; `GET /file?ref=` → attachment (reuse handler). Validate `key`/`ref`.
  - Check: curl/manual — `/api/threads` returns JSON; `/api/thread?key=…` returns messages; read endpoint flips unread.

## Phase 3 — Frontend two-pane SPA

- [ ] **Task 3.1: Layout + list pane**
  - Steps: HTML with header (📬 Inbox + unread badge + LIVE dot), left list, right reading pane. JS `loadList()` fetches `/api/threads`, renders rows (sender, subject, time); unread rows bold + ● ; set badge + `document.title='(N) Inbox'`. Diff-render to avoid flicker.
  - Check: list shows conversations; unread styled; badge correct (manual).

- [ ] **Task 3.2: Reading pane + mark read**
  - Steps: clicking a row → `openThread(key)` fetches `/api/thread?key=`, renders subject + messages (`#seq`, from, direction badge, time, body with show-more, attachment ⬇ links); then `POST /api/thread/read?key=` and update the row to read + decrement badge; highlight the selected row.
  - Check: open a conversation → messages 1→N show, attachments download, row becomes read, badge drops (manual).

- [ ] **Task 3.3: Live updates + notification**
  - Steps: poll `/api/threads` every ~3 s; on new unread, prepend rows + show a subtle "N new" toast + update badge/tab title; keep the open conversation intact; if the open thread got a new message, refresh its pane.
  - Check: send an email → new bold row + toast within ~3 s; reply → appears in the open thread (manual).

## Phase 4 — Polish + docs

- [ ] **Task 4.1: Polish**
  - Steps: empty state, relative/local time formatting, selected-row highlight, attachment count chip on list rows, favicon/tab title. Keep CSS clean/dark (match existing dashboard).
  - Check: looks presentable; no console errors.

- [ ] **Task 4.2: Docs**
  - Steps: add an "Inbox UI" section to `docs/gmail-working-flow.md` (how to run `inbox_app.py`, the endpoints); note port 5001.
  - Check: commands accurate.

## Tracked Changes
_(filled during implementation)_

## References
- Spec: `docs/superpowers/specs/2026-06-17-gmail-inbox-ui-design.md`
- Working flow: `docs/gmail-working-flow.md`
- Store: `src/mailflow/persistence/sqlite_store.py`
- Feed dashboard (pattern to reuse): `scripts/showcase_dashboard.py`
