---
date: 2026-06-17
topic: Gmail-like inbox UI for mailflow
status: approved
related:
  - docs/gmail-working-flow.md
  - src/mailflow/persistence/sqlite_store.py
  - scripts/showcase_dashboard.py
---

# Gmail-like inbox UI (design)

## Overview
Turn the live conversation dashboard into a **Gmail-style inbox**: a two-pane viewer
where the left pane lists conversations (with unread indicators) and clicking one opens
it in the right reading pane, showing the thread's messages in order with downloadable
attachments. Read/unread is tracked; new mail surfaces as an unread badge.

## Goals
- Two-pane SPA: conversation list (left) + reading pane (right).
- **Full read/unread**: unread conversations bold + ● dot, header unread count, opening
  a conversation marks it read.
- Live updates (poll) with a "new mail" indicator and tab-title count.
- Attachments downloadable from the reading pane.
- Self-contained: stdlib `http.server` + vanilla JS, reading from `SqliteEmailStore`.

## Non-goals (YAGNI)
Compose/reply/send (read-only viewer), search/labels/folders, auth/multi-user, websockets
(polling is fine). These can come later.

## Architecture
- **New app:** `scripts/inbox_app.py` (the Gmail-like viewer). The existing
  `showcase_dashboard.py` (simple live feed) stays as-is.
- **Backend:** stdlib `http.server` serving a JSON API + the SPA, backed by
  `SqliteEmailStore`. The Pub/Sub consumer (`run_gmail_live.py`) keeps filling the DB.
- **Frontend:** one HTML page + vanilla JS (no build step).

### Endpoints
| Method/Path | Returns |
|---|---|
| `GET /` | the SPA HTML |
| `GET /api/threads` | `{unread_total, threads:[{thread_key, subject, last_from, last_date, count, unread}]}` newest-active first |
| `GET /api/thread?key=…` | one conversation: `{thread_key, subject, messages:[{seq, from, direction, date, body, attachments}]}` |
| `POST /api/thread/read?key=…` | marks that conversation read; returns `{unread_total}` |
| `GET /file?ref=…&name=…&type=…` | attachment download (existing) |

### Store changes (`SqliteEmailStore`)
- Add **`read INTEGER DEFAULT 0`** column + migration (`ALTER TABLE … ADD COLUMN`).
- `save()` stores new emails as `read=0`.
- `thread_list()` → summaries with `unread` (1 if any message unread) + `unread_total`.
- `thread(thread_key)` → one conversation's messages (ordered 1st→latest).
- `mark_read(thread_key)` → `UPDATE emails SET read=1 WHERE thread_key=?`.
- Keep `threads()`, `recent()`, `count()`.

### Read/unread + notification behavior
- New email → unread; list row bold + ●; header badge + tab title `(N) Inbox`.
- Left pane polls `/api/threads` every ~3 s; diff-render (no flicker); a subtle toast on
  newly-arrived unread.
- Click a row → load `/api/thread`, render reading pane, `POST …/read`, decrement badge.

## Testing
- Store: TDD unit tests for `thread_list` (unread flags + total), `thread(key)`,
  `mark_read`. (Endpoints are thin wrappers over the store + a manual live check.)
- Full offline suite stays green; `mypy --strict` clean.

## Key decisions
- **Two-pane** layout, **full read/unread**, **stdlib http.server + vanilla JS** (no deps).
- New `inbox_app.py` rather than overwriting the feed dashboard.
