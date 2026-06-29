# mailflow — Project Showcase

**A complete walkthrough: what the project is, how it works end-to-end, the Google Cloud
keys, the data at every step (token → historyId → payload → raw → CleanEmail), the clean
process, and the full filter options — with diagrams and real data.**

---

## Contents
1. [What is mailflow](#1-what-is-mailflow)
2. [Architecture (ports & adapters)](#2-architecture-ports--adapters)
3. [The overall working flow](#3-the-overall-working-flow)
4. [Google Cloud — the keys & config](#4-google-cloud--the-keys--config)
5. [Step 1 — Connect (OAuth → access token)](#5-step-1--connect-oauth--access-token)
6. [Step 2 — Watch (subscribe for push)](#6-step-2--watch-subscribe-for-push)
7. [Step 3 — The notification payload (historyId)](#7-step-3--the-notification-payload-historyid)
8. [Step 4 — Fetch the real message](#8-step-4--fetch-the-real-message)
9. [Step 5 — The raw message (before clean)](#9-step-5--the-raw-message-before-clean)
10. [Step 6 — Filter](#10-step-6--filter)
11. [Step 7 — Extract → CleanEmail](#11-step-7--extract--cleanemail)
12. [Step 8 — Emit to your app](#12-step-8--emit-to-your-app)
13. [Filter options — the full set](#13-filter-options--the-full-set)
14. [The CleanEmail data model](#14-the-cleanemail-data-model)
15. [Reliability](#15-reliability)
16. [Technology stack](#16-technology-stack)
17. [Status](#17-status)

---

## 1. What is mailflow

A reusable library that does ONE job for many projects:

> **Connect to an email inbox → throw away the junk → turn each useful email into ONE
> consistent format (`CleanEmail`) → hand it to whatever application needs it.**

```
   📬 Inbox (Gmail / Outlook)  ──▶  📦 mailflow  ──▶  ✨ CleanEmail  ──▶  🖥️ your app
                                    (filter + clean)                     (store / AI / notify)
```

The consuming app never touches Gmail's API, OAuth, Pub/Sub, MIME parsing, attachments,
dedup, or reliability — it just receives a clean object.

---

## 2. Architecture (ports & adapters)

The core depends only on **interfaces (ports)** — never on vendor code. Each provider is an
**adapter** that fits the port. Swap Gmail↔Outlook by writing one adapter; the core never
changes.

```
        ┌──────────────── CORE (pure Python, no vendor SDKs) ────────────────┐
        │   pipeline: claim → size → parse → filter → extract → emit          │
        │   CleanEmail contract   ·   §8 correctness invariants               │
        │   ports: Provider · Parser · Filter · Extractor · Emitter · Stores   │
        └───────────────┬────────────────────────────────┬───────────────────┘
                        │                                 │
              GMAIL ADAPTER  ✅ live                GRAPH (OUTLOOK) ADAPTER ⚠️ code-done
              OAuth + Cloud Pub/Sub                 MSAL + Azure Event Hubs
```

---

## 3. The overall working flow

```
   ┌─────────┐                                              ┌──────────────────┐
   │  YOUR   │  connect("gmail", credentials, mailbox)      │   Google OAuth    │
   │  APP    │ ───────────────────────────────────────────▶│   Gmail API       │
   └────┬────┘                                              │   Cloud Pub/Sub   │
        │  for email in mf.stream():                        └────────┬─────────┘
        │      my_app.save(email)                                    │
        ▼                                                            │
   ╔════════════════════ INSIDE mailflow (the library) ═════════════╪══════════╗
   ║                                                                  ▼          ║
   ║  ① CONNECT   OAuth refresh-token → access-token ◀──────── token server     ║
   ║  ② WATCH     users.watch(mailbox, topic) ───────────────▶ Gmail            ║
   ║  ③ NOTIFY    Pub/Sub push {emailAddress, historyId} ◀──── on new mail      ║
   ║  ④ FETCH     history.list (what changed) + messages.get(raw)               ║
   ║  ⑤ PARSE     envelope (sender/subject/headers)                             ║
   ║  ⑥ FILTER    keep / drop / uncertain                                       ║
   ║  ⑦ EXTRACT   raw RFC822 → CleanEmail (decode body + attachments)           ║
   ║  ⑧ EMIT      hand the CleanEmail to your app                               ║
   ╚════════════════════════════════════════════════════════════════════════════╝
```

The app touches only the **top** (`connect`) and the **bottom** (`for email in …`). Steps
①–⑧ are the engine.

---

## 4. Google Cloud — the keys & config

Two groups: OAuth (to READ Gmail) + Pub/Sub (to RECEIVE notifications).

| Key | What it is | Type | Where to get it |
|---|---|---|---|
| `GMAIL_CLIENT_ID` | OAuth client id | string | Cloud Console → OAuth client |
| `GMAIL_CLIENT_SECRET` | OAuth client secret | string (secret) | same |
| `GMAIL_REFRESH_TOKEN` | per-mailbox consent token | string (secret) | one-time consent flow |
| `GMAIL_MAILBOXES` | mailbox to watch | string | the address, or `me` |
| `PUBSUB_PROJECT_ID` | GCP project | string | Cloud Console |
| `PUBSUB_TOPIC` | topic Gmail pushes to | string | Pub/Sub → Topics |
| `PUBSUB_SUBSCRIPTION` | subscription you consume | string | Pub/Sub → Subscriptions |
| `GOOGLE_APPLICATION_CREDENTIALS` | service-account JSON path | file path | Pub/Sub subscriber auth |

> **Secrets are references, never literals:** the code passes `"env://GMAIL_CLIENT_SECRET"`,
> resolved at runtime. Nothing sensitive is hardcoded.

**One-time Google Cloud setup** (the slow part — done once):
```
   1. Create a GCP project
   2. Create an OAuth client            → client_id + client_secret
   3. Create a Pub/Sub topic + subscription
   4. Grant gmail-api-push@system.gserviceaccount.com  Publisher on the topic  ← easily missed!
   5. Consent once for the mailbox      → refresh_token
```

---

## 5. Step 1 — Connect (OAuth → access token)

```
   INPUT:   client_id + client_secret + refresh_token
   CALL:    POST https://oauth2.googleapis.com/token
   OUTPUT:  access_token   (string, ~1 hour life, auto-refreshed)
```

The refresh token is permanent; the access token is short-lived. The library swaps one for
the other automatically (`OAuthTokenProvider`).

---

## 6. Step 2 — Watch (subscribe for push)

```
   CALL:    POST /users/{mailbox}/watch
            body = { "topicName": "projects/.../topics/gmail-notifications",
                     "labelIds": ["INBOX"] }
   OUTPUT:  { "historyId": "464008", "expiration": "..." }   ← the STARTING cursor
```

After this, Gmail pushes a notification to your topic on every new email. The returned
`historyId` is the starting bookmark (the cursor).

---

## 7. Step 3 — The notification payload (historyId)

When mail arrives, Google pushes this to Pub/Sub — a **tiny pointer, no content**:

```json
{
  "emailAddress": "jeevananthan.p@techjays.com",
  "historyId": 464081
}
```

| Field | Value | Type | Meaning |
|---|---|---|---|
| `emailAddress` | `"jeevananthan.p@techjays.com"` | string | which mailbox changed |
| `historyId` | `464081` | integer | watermark — "changed up to here" |

```
   parse_pubsub_message(data)  →  ("jeevananthan.p@techjays.com", 464081)
```

The notification has **no email** — only a watermark. The library now goes and fetches.

---

## 8. Step 4 — Fetch the real message

Two API calls turn the watermark into the actual email:

```
   ④a  GET /users/{mailbox}/history?startHistoryId=464008&historyTypes=messageAdded
       RESPONSE:  changed message IDs = ["19ef7f50b0cf150c"]
                  new cursor historyId = 464081
```
```
   ④b  GET /users/{mailbox}/messages/19ef7f50b0cf150c?format=raw
       RESPONSE:  { "raw": "<62264 chars base64url>", "sizeEstimate": 46697 }
```

- `history.list` (④a) says *which* messages changed since the stored cursor.
- `messages.get` (④b) returns the *actual* message — the full RFC822, base64url-encoded.

```
   Pub/Sub gave you:   a POINTER (historyId)
   history.list gave:  WHICH messages (ids)
   messages.get gave:  the ACTUAL message (raw bytes)
```

---

## 9. Step 5 — The raw message (before clean)

Decoding the base64 gives the real RFC822 message (46,697 bytes) — headers + body + the
attachment, all inline. Real example structure:

```
   MIME TREE:
   multipart/mixed
    ├── multipart/alternative
    │    ├── text/plain   → "new test this one"
    │    └── text/html    → "<div>new test this one</div>"
    └── text/markdown [base64]  → email-processing-architecture.md  (28,700 bytes)  ← attachment

   KEY HEADERS (the routing/DKIM/SPF soup is skipped):
   From:        Jeeva <jeevaskp1308@gmail.com>
   To:          jeevananthan.p@techjays.com
   Subject:     new test
   Date:        Wed, 24 Jun 2026 10:18:02 +0530
   Message-ID:  <CAF3Ow7RkEa7MH1F9PXTfNuEYsdXm6-gAw+...@mail.gmail.com>
   Content-Type: multipart/mixed; boundary="..."
```

This messy, nested format is what the EXTRACT step cleans up.

---

## 10. Step 6 — Filter

Before the expensive extract, the cheap envelope (sender/subject/headers) runs through the
filter chain — **keep / drop / uncertain**. Junk is killed before any work.

```
   Envelope ──▶ filter 1 ──▶ filter 2 ──▶ ...   (first KEEP or DROP wins; else pass)
   default = [] → nothing dropped (safe-by-default)
```

(Full filter options in §13.)

---

## 11. Step 7 — Extract → CleanEmail

The raw RFC822 is parsed into the flat, predictable `CleanEmail`. Real output:

```json
{
  "schema_version": "1.0",
  "tenant": "me",
  "email": {
    "canonical_id": "<CAF3Ow7RkEa7MH1F9PXTfNuEYsdXm6-gAw+...@mail.gmail.com>",
    "provider": "gmail",
    "provider_message_id": "19ef7f50b0cf150c",
    "direction": "inbound",
    "from": { "name": "Jeeva", "address": "jeevaskp1308@gmail.com" },
    "to":   [ { "address": "jeevananthan.p@techjays.com" } ],
    "subject": "new test",
    "body_text": "new test this one",
    "body_html": "<div dir=\"ltr\">new test this one</div>",
    "attachments": [
      {
        "filename": "email-processing-architecture.md",
        "content_type": "text/markdown",
        "size_bytes": 28700,
        "content_hash": "9139224f...12fe48c",
        "storage_ref": "9139224f...12fe48c"     // POINTER to the blob, NOT the bytes
      }
    ],
    "message_size_bytes": 46732,
    "raw_headers": { "...": "full original header map" }
  }
}
```

### The raw → clean transformation (before / after)

```
  RAW RFC822 (messy)                        CleanEmail (clean)
  ─────────────────────────────────────────────────────────────────────────
  50 lines of ARC/DKIM/SPF headers     →    raw_headers (kept, tucked away)
  From: Jeeva <jeevaskp1308@gmail.com> →    from = {name:"Jeeva", address:"jeeva…"}
  Subject: new test                    →    subject = "new test"
  multipart/alternative                →    body_text / body_html
  text/markdown [base64 28700 bytes]   →    attachments[0] {filename, size, storage_ref}
                                            (bytes streamed to blob, pointer kept)
```

**Cleaning = parse the MIME tree → flat fields → decode base64 → stream attachment bytes to
storage.** Your app never sees the mess.

---

## 12. Step 8 — Emit to your app

The `CleanEmail` is handed to your app — three ways:

```python
for email in mf.stream(): ...               # pull loop
connect("gmail", ..., on_email=handle).run()  # callback
emails = mf.fetch_new()                     # batch
```

---

## 13. Filter options — the full set

`filters` is one list; entries run in order; first KEEP/DROP wins; default `[]` = nothing
dropped (safe). Verified live against the inbox
`alice@techjays · bob@gmail · carol@spam · dave@partner`:

| `kind` | Effect | Demo result (kept) |
|---|---|---|
| (none) | safe default | all 4 |
| `only_domain` `["techjays.com"]` | keep ONLY that domain | alice |
| `only_sender` `["dave@partner.com"]` | keep ONLY that person | dave |
| `blacklist` `["spam.com"]` | drop those domains | all except carol |
| `no_personal` | drop gmail/yahoo/… | all except bob |
| `subject` `["(?i)invoice"]` | drop by subject regex | all except alice |
| `whitelist` `["techjays.com"]` | keep target, **others still pass** | all 4 |
| `internal_domain` | drop your own org | (per config) |
| `list_mail` | drop newsletters / auto-replies | (header-based) |
| custom `lambda env: …` | any rule (True=keep) | your logic |

### ⚠️ whitelist vs only_domain (the key distinction)
```
  whitelist techjays   → kept all 4   (others NOT excluded — it only KEEPS matches)
  only_domain techjays → kept alice   (others DROPPED — "only this domain")
```

### Filter vs Stage (which to use)
```
  sender · subject · recipient · headers  → FILTER (cheap, on the Envelope, BEFORE extract)
  body · attachments · size · direction   → STAGE  (rich, on the CleanEmail, AFTER extract)
```

### Configuring
```python
connect("gmail", credentials=..., filters=[
    {"kind": "only_domain", "domains": ["company.com"]},   # which emails
    lambda env: "invoice" in env.subject.lower(),           # custom rule
], fields=["subject", "from", "attachments"],               # which data
   stages=[lambda e: e if e.attachments else None])         # process each
```

---

## 14. The CleanEmail data model

```
  IDENTITY     canonical_id · message_id · provider · provider_message_id
  PEOPLE       from_ · sender · reply_to · to · cc · bcc        (each {name, address})
  CONTENT      subject · body_text · body_html · body_truncated
  ATTACHMENTS  filename · content_type · size_bytes · content_hash(sha256) ·
               is_inline · storage_ref(pointer, NOT bytes)
  CLASSIFY     direction(inbound/outbound) · is_draft · labels · categories · folder
  HEADERS      list_id · list_unsubscribe · auto_submitted · raw_headers(full map)
  META         date_utc · received_at · message_size_bytes · schema_version("1.0")
```

One contract, same shape from Gmail or Outlook — write your app once.

---

## 15. Reliability

A long-running service stays healthy automatically:

```
  WATCH RENEWAL        renews daily → notifications never stop (watches expire ~7 days)
  STALE-CURSOR RECOVERY re-seeds on a too-old historyId (Gmail 404) → never stuck
  SWEEP                 periodic poll catches anything a push missed
  AT-LEAST-ONCE         atomic claim de-dupes in-system; idempotency_key lets consumers drop repeats
  DLQ                   poison/oversized → dead-lettered with a reason; never blocks the rest
```

---

## 16. Technology stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| Models / validation | Pydantic v2 |
| Type safety | mypy (strict) |
| Tests | pytest — 97 tests (unit, §8 invariants, end-to-end Gmail, edge cases) |
| Packaging | hatchling → wheel; light core + `[gmail]` / `[graph]` extras |
| Email parsing | Python `email` stdlib (MIME / RFC822) |
| Persistence | SQLite (stdlib) + in-memory |
| **Gmail** | Google OAuth 2.0 · Gmail API · **Cloud Pub/Sub** |
| **Outlook** | MSAL · Microsoft Graph · **Azure Event Hubs** |

---

## 17. Status

```
  ✅ Gmail flow            DONE + proven live (real email → CleanEmail)
  ✅ Library API           connect + filters + fields + stages + retrieval
  ✅ Reliability           watch renewal · 404 recovery · sweep · at-least-once (idempotency_key)
  ✅ Quality               97 tests · mypy strict clean · full docs
  ⚠️ Outlook (Graph)        code-complete; needs a live Exchange mailbox to validate
  ⏸️ Publish / SaaS         deferred — later-stage decisions
```

---

*The whole project in one line: **mailflow turns the messy, multi-step job of watching an
inbox into a single clean object your app receives in ~15 lines — connect, filter, get
CleanEmail.***
