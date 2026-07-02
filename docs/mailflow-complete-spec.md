# mailflow — Complete Specification & Flow Guide

**One document, start to end.** What mailflow is, how it's built, every flow with the exact
data that moves through it, and how a user actually uses it. Written for both a **developer**
(the tech) and a **user** (the experience). Every diagram is ASCII so it reads anywhere.

> Reflects the current `feature/library` build — wire event **schema 1.3**.

---

## Table of contents

```
   Part 0   What mailflow is (30 seconds)
   Part 1   The big picture — overall diagram
   Part 2   Architecture — what type of system this is
   Part 3   The two phases — setup (once) + runtime (per email)
   Part 4   The master end-to-end flow (with data at every step)
   Part 5   Each flow in detail — DATA SENT → DATA RECEIVED
   Part 6   Always-on reliability flows
   Part 7   User flows — single person · many people · whole domain
   Part 8   How you consume — stream / fetch_new / on_email
   Part 9   Configuration reference — the knobs
   Part 10  Data structures — what an email looks like at each stage
   Part 11  Technology stack
   Appendix Quick reference
```

---

# Part 0 — What mailflow is (30 seconds)

**Plain English (user):**
> mailflow watches an email inbox, throws away the junk you don't want, cleans up what's
> left, and hands your program one tidy email object — so you never touch raw email, OAuth,
> or attachments yourself.

**Technical (developer):**
> A provider- and transport-agnostic email-ingestion library. Its core depends only on
> Python `Protocol` ports — no vendor SDKs — so Gmail (live) and an in-memory source (tests)
> plug into the same pipeline. It guarantees exactly-once processing, ordered cursors,
> dead-lettering, and attachment safety, and emits a validated `CleanEmail`.

**The one line:**
```
   📬 inbox  ─▶  📦 mailflow (connect · filter · clean)  ─▶  ✨ one CleanEmail  ─▶  🖥️ your app
```

---

# Part 1 — The big picture (overall diagram)

```
   ┌──────────┐        ┌─────────────────────────────────────────────┐        ┌──────────┐
   │  GMAIL   │        │                  mailflow                    │        │ YOUR APP │
   │  inbox   │        │                                              │        │          │
   └────┬─────┘        │   ① CONNECT   OAuth → access token           │        └────▲─────┘
        │              │   ② WATCH     subscribe for push             │             │
        │  new mail    │   ③ NOTIFY    receive Pub/Sub ping (pointer) │             │
        ├─────────────▶│   ④ FETCH     get the real message (raw)     │             │
        │              │   ⑤ PARSE     sender / subject / headers      │             │
        │              │   ⑥ FILTER    keep / drop                     │  CleanEmail │
        │              │   ⑦ EXTRACT   decode body + attachments       │─────────────┘
        │              │   ⑧ EMIT      hand off the CleanEmail         │
        │              └─────────────────────────────────────────────┘
        │                                   │
        │                                   ▼  (attachment bytes)
        │                        📁 blob store (folder / S3)   +   🗄️ emails.db (text + pointers)
```

```
   KEY:  Pub/Sub carries a POINTER (historyId) — never the email.
         history.list says WHICH messages (ids).
         messages.get gives the ACTUAL message (raw RFC822 bytes).
         extract gives the CLEAN object (CleanEmail).
```

---

# Part 2 — Architecture (what type of system this is)

**Type: Ports & Adapters (hexagonal) event-driven pipeline.**

```
   ┌──────────────────── CORE (no vendor code) ─────────────────────┐
   │   pipeline · CleanEmail · ports (Protocols) · filters · identity │
   └───────────────┬────────────────────────────┬───────────────────┘
       PROVIDERS    │                            │   EMITTERS (output)
       ┌────────────┴───────────┐    ┌───────────┴────────────┐
       │ gmail   (live)         │    │ stream() / fetch_new()  │
       │ graph   (code-done)    │    │ on_email (callback)     │
       │ memory  (tests/demos)  │    │ queue / webhook / sqlite│
       └────────────────────────┘    └─────────────────────────┘
       STORES:  cursor  ·  dedupe  ·  blob (attachments)  ·  dead-letter
```

**Why this shape:** the core talks only to interfaces (`Protocol`s), so you swap Gmail for
Outlook, or a local folder for S3, or the live source for a fake one — **without touching the
core**. That is why the same filters/stages/attachment code runs identically in tests
(`"memory"`) and in production (`"gmail"`).

---

# Part 3 — The two phases

```
   ═══ PHASE 1 · SETUP  (happens ONCE, when you call connect) ═══════════════

      YOUR APP ──connect("gmail", credentials)──▶ mailflow
                                                    │
                              ① refresh-token → access-token   (OAuth)
                              ② users.watch(mailbox, topic) → starting historyId
                              ③ subscribe to Pub/Sub
                                                    │
                                            "Waiting for mail…"

   ═══ PHASE 2 · RUNTIME  (repeats automatically, per email) ════════════════

      new email ─▶ Gmail ─push─▶ Pub/Sub ─▶ mailflow ─▶ fetch ─▶ filter ─▶ clean ─▶ YOUR APP
```

---

# Part 4 — The master end-to-end flow (with data at every step)

```
   STEP        WHAT HAPPENS                       DATA IN                 DATA OUT
   ──────────────────────────────────────────────────────────────────────────────────────────
   ① CONNECT   exchange credentials               client_id+secret+       access_token
                                                  refresh_token           (string, ~1h)

   ② WATCH     register push for the mailbox       POST users/{mbx}/watch  { historyId: 464008 }
                                                  + topic name            (starting cursor)

   ③ NOTIFY    Pub/Sub delivers a pointer          (push from Gmail)       { emailAddress,
                                                                            historyId: 464081 }

   ④ FETCH     ask which msgs, then get one         startHistoryId=464008  ["19ef7f50b0cf150c"]
                                                  + messages.get(raw)      { raw:<base64>, size }

   ⑤ PARSE     read the cheap headers              raw RFC822 bytes        Envelope {from,subject,
                                                                            to,cc,headers}

   ⑥ FILTER    keep / drop decision                Envelope                KEEP · DROP · UNCERTAIN

   ⑦ EXTRACT   decode into a clean object          raw RFC822 bytes        CleanEmail
                                                                            (body + attachments)

   ⑦b ATTACH   stream files → blob store            attachment MIME parts   storage_ref (sha256)
                                                                            + metadata

   ⑧ STAGES    transform / drop each               CleanEmail              CleanEmail | None

   ⑨ EMIT      hand off to your app                EmailEvent(email)       your stream()/on_email
   ──────────────────────────────────────────────────────────────────────────────────────────
   ALWAYS:  dedupe-claim before ④  ·  cursor advances after ⑨  ·  poison/oversized → DLQ
```

---

# Part 5 — Each flow in detail (DATA SENT → DATA RECEIVED)

## 5.1 · Setup — get a refresh token (one-time, per person)

```
   USER runs:   mailflow auth gmail
        │
   SEND ▶  client_id + client_secret + scope (gmail.readonly)  →  Google consent screen
        │   (browser opens, user clicks "Allow")
   RECV ◀  REFRESH TOKEN  ← saved to .env (0600 owner-only)
   TECH:   google-auth-oauthlib · InstalledAppFlow · local redirect :8080
```

## 5.2 · Setup — register the watch

```
   SEND ▶  POST /users/{mailbox}/watch  { topicName, labelIds:["INBOX"] }
           Authorization: Bearer <access_token>          (from the refresh token)
   RECV ◀  { historyId: "464008", expiration }           ← the STARTING cursor
   EFFECT: Gmail now PUSHES a notification to your topic on every new email.
   TECH:   Gmail API users.watch · expires ~7 days · library auto-renews daily
```

## 5.3 · Runtime — the Pub/Sub notification (the pointer)

```
   SEND ▶  (nothing — Gmail pushes)
   RECV ◀  Pub/Sub message:  { message: { data: "<base64>" } }
           base64-decode(data) = { "emailAddress": "you@x.com", "historyId": 464081 }
   NOTE:   ~60 bytes. NO subject, NO body, NO attachment — just a POINTER saying
           "mailbox X changed, new historyId 464081."
   TECH:   Google Cloud Pub/Sub pull subscription · service-account JSON auth
```

## 5.4 · Runtime — fetch (pointer → which messages → the message)

```
   SEND ▶  GET /users/{mbx}/history?startHistoryId=464008          (what changed?)
   RECV ◀  ["19ef7f50b0cf150c"]                                    (message IDs)
        │
   SEND ▶  GET /users/{mbx}/messages/19ef7f50b0cf150c?format=raw   (get it)
   RECV ◀  { raw: "<base64 RFC822>", sizeEstimate: 46697 }         (the ACTUAL email)
   TECH:   Gmail API history.list + messages.get · httpx · backoff on 429/5xx
   GUARD:  size checked BEFORE download — >50 MB or unknown → DLQ, never fetched
```

## 5.5 · Runtime — parse the envelope (cheap, pre-body)

```
   SEND ▶  raw RFC822 bytes
   RECV ◀  Envelope { from_, subject, to[], cc[], canonical_id,
                      provider_message_id, is_auto_submitted, is_bounce }
   WHY:    the Envelope is the CHEAP header view — enough to decide keep/drop
           WITHOUT decoding the full body/attachments yet.
   TECH:   Python stdlib email parser (headers only)
```

## 5.6 · Runtime — filter (keep / drop)

```
   SEND ▶  Envelope  →  filter chain
   RECV ◀  one of:  KEEP (accept, stop) · DROP (reject, stop) · UNCERTAIN (try next)

           filter 1 ─┐   first KEEP or DROP wins;
           filter 2 ─┤   if all UNCERTAIN → the email passes (safe-by-default)
           filter 3 ─┘

   FILTERS: blacklist · whitelist · only_domain · only_sender · block_sender ·
            internal_domain · no_personal · subject · to · cc · list_mail · lambda
   DROPPED HERE: never gets its body/attachments decoded — cheap rejection.
```

## 5.7 · Runtime — extract (raw → CleanEmail)

```
   SEND ▶  raw RFC822 bytes
   RECV ◀  CleanEmail {
              canonical_id, message_id, from_, to[], cc[], subject,
              body_text, body_html, attachments[], stripped_attachments[],
              date_utc, direction, thread_key, is_auto_submitted, is_bounce,
              schema_version="1.3" }
   TECH:   MimeExtractor · decodes base64/quoted-printable/charset · HTML→text fallback
```

## 5.8 · Runtime — attachments (stream → store → pointer)

```
   IN THE EMAIL:  each attachment is base64 TEXT inside the MIME body.
   SEND ▶  attachment MIME part
   STEPS:  decode base64 → original file bytes → sha256(bytes)=content_hash →
           stream bytes to blob store → attachments/<hash>
   RECV ◀  Attachment {
              filename, content_type, size_bytes, content_hash,
              is_inline, storage_ref = <hash> }        ← a POINTER, not the bytes
   RULES:  25 MB per-part cap (default fail-closed → DLQ); with a policy → strip & deliver.
           Same file across 2 emails = stored ONCE (content-addressed dedupe).
   STORE:  emails.db row = metadata + storage_ref   ·   attachments/<hash> = the real bytes
```

## 5.9 · Runtime — stages / clean (process each)

```
   SEND ▶  CleanEmail
   RECV ◀  CleanEmail (modified)  OR  None (drop)
   clean_fn runs first (one polish hook), then stages[] in order.
   A stage returning None/False DROPS the email — e.g. drop bounces, tag subjects, enrich.
```

## 5.10 · Runtime — emit / deliver (hand off)

```
   SEND ▶  EmailEvent { schema_version:"1.3", tenant, ordering_key,
                        idempotency_key, email: CleanEmail }
   RECV ◀  YOUR app, one of three ways:
              stream()     → you loop and pull
              on_email(fn) → mailflow pushes to your function
              fetch_new()  → one batch as a list
   AFTER:  ack Pub/Sub (only now) · dedupe mark_done · cursor commit_if_ahead
```

---

# Part 6 — Always-on reliability flows (you get these for free)

```
   DEDUPE        claim idempotency_key BEFORE any work → same email never processed twice
   CURSOR        monotonic historyId bookmark, advances on EVERY outcome, restart-safe
   SIZE GUARD    >50 MB or unknown size → DLQ before download
   RETRY → DLQ   transient (429/5xx) → backoff retry ≤3 → DLQ · permanent (403/404) → DLQ now
   401 → REFRESH fetch-time 401 → force ONE token refresh + retry once, else DLQ
   DURABLE DLQ   poison/over-cap persisted → redrive() re-submits through a fresh pipeline
   ACK-AFTER     ack Pub/Sub only after success → crash = redelivery, no loss
   BLOB DEDUPE   identical attachment stored exactly once (content-addressed)
   WATCH RENEW   daily auto-renewal + 15-min sweep + 404 cursor re-seed
```

```
   ONE email's guarded life:
   pointer → CLAIM → size-guard → FETCH → parse → FILTER → EXTRACT → stages → EMIT
        │                                                                      │
   if dup → skip                                              ack · mark_done · cursor++
        └─ any poison / oversized at any point → DLQ (counted once, cursor still advances)
```

---

# Part 7 — User flows (the user-facing perspective)

## 7.1 · Single person (you / one mailbox)

```
   pip install mailflow[gmail]
   mailflow auth gmail                      # browser → Allow → token saved to .env
   # your code:
   from mailflow import connect
   connect("gmail").on_email(lambda e: print(e.subject))

   CREDENTIAL: ONE refresh token.   SCALES: just you.
```

## 7.2 · Multiple people (per-user tokens)

```
   run `mailflow auth gmail` ONCE PER PERSON → collect N refresh tokens
   connect("gmail",
       accounts=[{"mailbox":"alice@co.com","refresh_token":"env://ALICE"},
                 {"mailbox":"bob@co.com","refresh_token":"env://BOB"}], …)

   HOW: notification.emailAddress → look up THAT person's token → fetch their mail.
   CREDENTIAL: one token PER person.   USE: a few people, or no admin access.
```

## 7.3 · Whole domain (domain-wide delegation — company scale)

```
   Workspace admin authorizes ONE service account (client id + gmail.readonly scope).
   connect("gmail", service_account="sa.json", domain="co.com", mailboxes="all", …)

   HOW: the service account IMPERSONATES each mailbox (.with_subject) — no per-user consent.
   CREDENTIAL: ONE service account for everyone.   USE: whole company; add a person = 1 line.
```

```
   SHARED no matter how many people:  OAuth client · Pub/Sub topic · subscription ·
                                       service-account key · dedupe · cursor · blob · emitter
   PER PERSON:  a credential  +  a users.watch() call
```

---

# Part 8 — How you consume (the three modes)

```
   stream()      for email in mf.stream(): …        YOU pull, in a loop        (continuous)
   on_email      connect(on_email=fn); mf.run()     mailflow PUSHES to your fn  (event-driven)
   fetch_new()   emails = mf.fetch_new()            one batch → list           (run-once script)

   PULL vs PUSH:  stream = "give me the next"  ·  on_email = "here, handle this"
   Same emails, same pipeline — only who is in control differs.
```

---

# Part 9 — Configuration reference (the knobs)

```
   connect(provider, credentials, mailbox,
       filters=[…],     ─▶ WHICH emails reach you   (drop the rest, pre-body)
       fields=[…],      ─▶ WHICH data you receive    (trim to a dict)
       stages=[…],      ─▶ PROCESS each email         (modify / drop, post-extract)
       clean_fn=…,      ─▶ one polish hook (runs first)
       attachments=…,   ─▶ per-class size/type/scan policy (strip-and-deliver)
       state="sqlite:///…", ─▶ persist cursor+dedupe+blobs, resume after restart
       on_email=…,      ─▶ push callback mode
       overrides={…},   ─▶ swap any component (e.g. rotation_sink)
       verify_scope_on_startup=True,  ─▶ fail fast if gmail.readonly missing
   )
```

```
   PIPELINE ORDER:  provider → parse → FILTER → extract → CLEAN_FN → STAGES → fields → deliver
   FILTERS see the cheap Envelope (before body).   STAGES see the full CleanEmail.
```

---

# Part 10 — Data structures (what an email looks like at each stage)

```
   ① Pub/Sub notification   { emailAddress, historyId }                    ~60 bytes, a POINTER

   ② messages.get(raw)      { id, raw:<base64 RFC822>, sizeEstimate }      the real email bytes

   ③ Envelope               { from_, subject, to[], cc[], canonical_id,    cheap header view
                              provider_message_id, is_auto_submitted, is_bounce }

   ④ CleanEmail             { canonical_id, message_id, from_, to[], cc[], the clean object
                              subject, body_text, body_html,
                              attachments[], stripped_attachments[],
                              date_utc, direction, thread_key,
                              is_auto_submitted, is_bounce, schema_version:"1.3" }

   ⑤ Attachment             { filename, content_type, size_bytes,          metadata + POINTER
                              content_hash, is_inline, storage_ref }        (bytes in blob store)

   ⑥ EmailEvent (the wire)  { schema_version:"1.3", tenant, ordering_key,  what your app receives
                              idempotency_key, email: CleanEmail }
```

---

# Part 11 — Technology stack

```
   LANGUAGE     Python 3.12+ · Pydantic v2 (validation) · mypy --strict · pytest (419 tests)
   GMAIL        OAuth 2.0 refresh token · Gmail API (watch / history.list / messages.get) · httpx
   TRANSPORT    Google Cloud Pub/Sub (pull) · service-account JSON auth
   PARSING      Python stdlib email (RFC822/MIME) · base64 / quoted-printable / charset decode
   STORAGE      SQLite (cursor + dedupe + email rows) · local folder or S3/GCS (attachment blobs)
   PACKAGING    hatchling · pip install mailflow[gmail] · `mailflow` CLI + python -m mailflow
   RELIABILITY  idempotency claim · monotonic cursor · durable DLQ + redrive · backoff+jitter
```

---

# Appendix — Quick reference

```
   INSTALL      pip install "mailflow[gmail]"
   AUTH         mailflow auth gmail            (browser → Allow → token to .env, 0600)
   VERIFY       mailflow check gmail           (Gmail read + Pub/Sub consume OK)
   DEMO         connect("memory", seed=…)      (fake data, no setup — for tests/trying)
   LIVE         connect("gmail", credentials=…)(real inbox — needs cloud setup)

   THE 3 KNOBS  filters (which emails) · fields (which data) · stages (process each)
   THE 3 MODES  stream() (pull) · on_email (push) · fetch_new() (one batch)
   THE 3 SCALES single token · accounts map · domain-wide delegation

   ALWAYS-ON    dedupe · cursor · size-guard · retry→DLQ · durable-DLQ+redrive ·
                401→refresh-once · blob-dedupe · watch-renew · scope-check · metrics+health

   SETUP DOC    docs/google-setup-step-by-step.md   (Google Cloud + Pub/Sub, click by click)
   EXPLORER     feature-explorer-v2.html            (interactive, accurate API reference)
```

```
   THE WHOLE SYSTEM IN ONE PICTURE
   ───────────────────────────────
   SETUP (once)                RUNTIME (per email, automatic)
   connect()                   new mail
      ├─ OAuth                    │
      ├─ watch          Gmail ─push─▶ Pub/Sub ─▶ FETCH(raw)
      └─ subscribe                            │
                                              ▼
                            PARSE ─▶ FILTER ─▶ EXTRACT ─▶ STAGES ─▶ EMIT ─▶ YOUR APP
                                        │          │
                                     (drop)   attachments ─▶ 📁 blob store
                                                              🗄️ emails.db
                     ⏰ renew · sweep · 404-recover · dedupe · cursor · DLQ
```
