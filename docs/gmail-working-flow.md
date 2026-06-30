# mailflow — Gmail working flow (end-to-end)

A complete walkthrough of how email flows from a Gmail mailbox, through Google Cloud,
into the mailflow service, and out to the consuming application — with every
connection, component, and step. Written so a teammate can understand the whole system.

> **Status:** built and **verified live** — real emails (incl. attachments and
> reply threads) flow end-to-end into a SQL store and a live dashboard.

---

## 1. What it does (one line)

When an email arrives in a watched Gmail mailbox, mailflow automatically normalizes it
into a standard **`CleanEmail`** and hands it to your application — decoupled, durable,
and provider-agnostic (the same core also handles Microsoft Outlook).

---

## 2. Architecture (the full picture)

```
   ┌─────────────┐   new mail    ┌──────────────────────┐   {emailAddress,        ┌──────────────────────┐
   │   Gmail      │ ────────────▶ │  Google Cloud        │     historyId}          │  mailflow consumer    │
   │  mailbox     │   users.watch │  Pub/Sub topic       │ ──────────────────────▶ │  (run_gmail_live.py)  │
   │ (Exchange of │   (renew      │  "gmail-notifications"│   pull (SA key)        │                       │
   │  Google)     │    daily)     └──────────────────────┘                         │  1. parse watermark   │
   └─────▲───────┘                                                                 │  2. history.list diff │
         │  3. messages.get(format=raw)  (OAuth refresh token)                     │  3. fetch raw msg     │
         └──────────────────────────────────────────────────────────────────────  │  4. MimeExtractor     │
                                                                                    │     -> CleanEmail     │
                                                                                    │  5. PubSubEmitter     │
                                                                                    └───────────┬──────────┘
                                                                                                │ publish
                                                                                                ▼
   ┌────────────────────────────┐   subscribe (clean-email-sub)   ┌──────────────────────────────────────┐
   │  Your application           │ ◀────────────────────────────── │  Pub/Sub topic "clean-email"          │
   │  (email-intelligence)       │                                 │  (one CleanEmail JSON per message)    │
   │  - store in SQLite          │                                 └──────────────────────────────────────┘
   │  - group into threads       │
   │  - download attachments     │   (the showcase dashboard does all three at http://localhost:5000)
   └────────────────────────────┘
```

**The principle:** "thin notification now, fetch the body later." Gmail's push is only
a *watermark* (no content); the consumer re-fetches the authoritative message and
normalizes it. The output goes onto a **queue** so the app reads it at its own pace.

---

## 3. The two auth layers (important)

There are **two separate credentials** — they do different jobs:

| Layer | Authorizes | Credential |
|---|---|---|
| **Gmail access** (read mail, watch) | `users.watch`, `history.list`, `messages.get` | **per-user OAuth refresh token** (scope `gmail.readonly`) |
| **Pub/Sub access** (pull notifications) | reading the subscription | **service-account JSON key** (`GOOGLE_APPLICATION_CREDENTIALS`) |

The OAuth token does **not** grant Pub/Sub; the service-account key does **not** read
Gmail. Both are required.

---

## 4. Google Cloud connections (what's provisioned)

| Resource | Name (this project) | Purpose |
|---|---|---|
| GCP project | `email-intelligence-499611` | holds everything |
| APIs enabled | Gmail API, Cloud Pub/Sub API | mail + messaging |
| Inbound topic | `gmail-notifications` | Gmail publishes change watermarks here |
| Inbound subscription | `email-intelligence` (pull) | mailflow consumer reads watermarks |
| Publisher grant | `gmail-api-push@system.gserviceaccount.com` → **Pub/Sub Publisher** on `gmail-notifications` | lets Gmail write to the topic |
| OAuth client | Web app, redirect `http://localhost:8080/` | issues the refresh token |
| Service account | `email-intelligence@…iam.gserviceaccount.com` (+ JSON key) | the consumer's identity |
| SA roles | **Pub/Sub Subscriber** (read `email-intelligence`), **Pub/Sub Publisher** (write `clean-email`) | pull + publish |
| Output topic | `clean-email` | mailflow publishes finished `CleanEmail`s here |
| Output subscription | `clean-email-sub` (pull) | your app reads finished emails |

> Setup steps for all of this: see **`docs/google-setup-step-by-step.md`** and the
> DevOps handoff **`docs/gmail-devops-handoff.md`**.

---

## 5. The working flow, step by step

1. **Watch** — on startup the consumer calls `users.watch(topic="gmail-notifications",
   labelIds=["INBOX"])`, which returns a starting `historyId` (seeded as the cursor).
   The watch lasts ≤7 days and is renewed.
2. **Email arrives** in the mailbox.
3. **Gmail publishes** a tiny notification to `gmail-notifications`:
   `{ "emailAddress": "...", "historyId": 451475 }` — **no content**, just a watermark.
4. **Consumer pulls** the notification from the `email-intelligence` subscription (using
   the service-account key).
5. **Diff** — it calls `history.list(startHistoryId=<last cursor>)` to find exactly which
   message ids changed since last time.
6. **Fetch** — for each id, `messages.get(format=raw)` returns the full **RFC822** message
   (headers + body + attachments), authenticated with the OAuth token.
7. **Normalize** — the core **`MimeExtractor`** parses the RFC822 into a `CleanEmail`
   (sender, recipients, subject, body, direction, threading headers, attachments…).
8. **Attachments** — attachment bytes are streamed to a blob store and `storage_ref` is
   set so the file is downloadable.
9. **Emit** — the `PubSubEmitter` publishes the `CleanEmail` JSON to the **`clean-email`**
   topic. The cursor advances to the new `historyId`; the Pub/Sub message is **acked**.
10. **Consume** — your application (here, the dashboard) subscribes to **`clean-email-sub`**,
    receives each `CleanEmail`, **stores it in SQLite**, and **groups it into a thread**.

---

## 6. What the application receives — the `CleanEmail`

One consistent JSON object per email (`schema_version: "1.3"`):

```json
{
  "schema_version": "1.3", "tenant": "me", "ordering_key": "ops@…",
  "email": {
    "canonical_id": "<…@mail.gmail.com>", "message_id": "<…>",
    "in_reply_to": "<parent@…>", "references": ["<root@…>", "<…>"],
    "provider": "gmail", "provider_message_id": "19ed…", "provider_stream_id": "you@…",
    "direction": "inbound", "subject": "…",
    "from": {"name": "…", "address": "…"}, "to": [...], "cc": [...],
    "date_utc": "2026-…", "body_text": "…", "body_html": "…",
    "attachments": [{"filename": "invoice.pdf", "content_type": "application/pdf",
                     "size_bytes": 84213, "content_hash": "sha256…",
                     "is_inline": false, "storage_ref": "sha256…"}],
    "labels": [], "categories": [], "folder": "",
    "message_size_bytes": 7610, "schema_version": "1.3"
  }
}
```

- **Threading** — `references[0]` is the conversation root; replies share it, so messages
  group into conversations ordered 1st → latest.
- **Attachments** — `storage_ref` points to the stored file; downloadable from the UI.

---

## 7. Components (file map)

| Area | Path | Role |
|---|---|---|
| Gmail adapter | `src/mailflow/adapters/gmail/` | client, provider, watch, runtime, live glue |
| Core pipeline | `src/mailflow/core/pipeline.py` | claim → filter → extract → emit (provider-agnostic) |
| Extractor (reused) | `src/mailflow/extract/mime.py` | RFC822 → `CleanEmail` |
| Output emitter | `src/mailflow/emit/pubsub.py` | publish `CleanEmail` to `clean-email` |
| Persistence | `src/mailflow/persistence/sqlite_store.py` | store + thread grouping (SQLite) |
| Attachment store | `src/mailflow/stores/local_blob.py` | save attachment bytes |
| Run the service | `scripts/run_gmail_live.py` | watch + consume + emit |
| App / dashboard | `scripts/showcase_dashboard.py` | store + thread UI + downloads |
| Get token | `scripts/get_gmail_refresh_token.py` | one-time OAuth → refresh token |
| Check connection | `scripts/check_gmail_connection.py` | verify both auth layers |
| Inspect DB | `scripts/query_emails.py` | list stored emails |

---

## 8. Configuration (`.env`)

```
# Gmail (OAuth)
GMAIL_CLIENT_ID=…              GMAIL_CLIENT_SECRET=…(secret)
GMAIL_REFRESH_TOKEN=…(secret)  GMAIL_MAILBOXES=you@yourdomain.com
# Pub/Sub
PUBSUB_PROJECT_ID=email-intelligence-499611
PUBSUB_TOPIC=gmail-notifications      PUBSUB_SUBSCRIPTION=email-intelligence
CLEAN_EMAILS_TOPIC=clean-email        CLEAN_EMAILS_SUBSCRIPTION=clean-email-sub
GOOGLE_APPLICATION_CREDENTIALS=…path to SA JSON key (secret)
EMAIL_DB=emails.db
```
Secrets are git-ignored; never commit `.env`, the SA key, or the refresh token.

---

## 9. How to run

```powershell
pip install -e ".[gmail]"
# load .env into the terminal (PowerShell):
Get-Content .env | ? {$_ -match '=' -and -not $_.StartsWith('#')} | % { $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k.Trim(),$v.Trim()) }

python scripts/check_gmail_connection.py     # 1. verify keys -> "ALL CHECKS PASSED"
python scripts/run_gmail_live.py             # 2. start the consumer (watch + ingest + publish)
python scripts/showcase_dashboard.py         # 3. start the app: http://localhost:5000
# send an email to the watched mailbox -> it appears as a conversation, stored in SQLite
python scripts/query_emails.py               # inspect the SQL store anytime
```

---

## 9a. Gmail-like Inbox UI (`scripts/inbox_app.py`)

A two-pane email viewer (like Gmail) over the SQLite store:
- **Left:** conversation list — unread shown **bold with a ● dot**, an **unread count badge**
  in the header, newest-active first.
- **Right:** click a conversation → reading pane shows its messages **1 → N** in order
  (sender, inbound/outbound tag, time, expandable body, ⬇ attachment downloads).
- **Read/unread:** opening a conversation marks it read (badge drops); new mail surfaces
  live (poll every ~3 s) with a "N new" toast and a tab-title count `(3) Inbox`.

Run it **instead of** `showcase_dashboard.py` (both consume `clean-email-sub`):
```powershell
python scripts/inbox_app.py      # then open http://localhost:5001
```
Endpoints: `GET /api/threads` (list + unread_total), `GET /api/thread?key=` (one
conversation), `POST /api/thread/read?key=` (mark read), `GET /file?ref=` (attachment).
Read/unread is a `read` column on the `emails` table; grouping uses `thread_key`.

## 10. Reliability notes

- **7-day watch:** `users.watch` expires after ≤7 days — renew daily so it never lapses.
- **Gap recovery:** emails are never lost (they live in the mailbox). On reconnect,
  `history.list(startHistoryId=<last cursor>)` backfills the gap; if the cursor is too
  old (HTTP 404), a full sync via `messages.list` recovers it. Dedupe (`canonical_id`)
  makes re-fetching idempotent.
- **Decoupling:** the `clean-email` queue means if your app is slow or down, emails wait
  safely in the subscription instead of being lost.
- **Recommended hardening for production:** persistent cursor store (instead of in-memory),
  a daily watch-renewal scheduler, and the 404 → full-sync fallback.

---

## 11. Cost

Gmail API + Pub/Sub are effectively **$0** at low volume (generous free tiers). No
always-on paid resource. SQLite storage is a local file. (The Microsoft/Event Hubs path,
by contrast, has an always-on namespace cost.)

---

## 12. Why this design

- **Provider-agnostic:** the same core pipeline + `MimeExtractor` + `CleanEmail` serves
  Gmail and Outlook — adding a provider is one adapter, no core changes.
- **Queue at the seam:** durable, decoupled hand-off to the consuming app.
- **Personal-Gmail friendly:** per-user OAuth means it works on a normal mailbox, no
  special infrastructure on the mail side.
