# mailflow — High-Level Flow Diagrams

Clean visual diagrams of the whole system: the big picture, the steps, each flow, and
what's involved at every stage.

---

## 1. The big picture (one line)

```
   📬 INBOX            📦 mailflow              ✨ CleanEmail          🖥️ YOUR APP
   Gmail / Outlook  ─▶  connect · filter · clean  ─▶  one clean object  ─▶  store / AI / notify
```

---

## 2. The high-level working flow

```
   ┌──────────┐        ┌───────────────────────────────────────────┐        ┌──────────┐
   │  GMAIL   │        │                 mailflow                   │        │ YOUR APP │
   │  inbox   │        │                                            │        │          │
   └────┬─────┘        │   ① CONNECT    OAuth → access token        │        └────▲─────┘
        │              │   ② WATCH      subscribe for push          │             │
        │  new mail    │   ③ NOTIFY     receive Pub/Sub ping        │             │
        ├─────────────▶│   ④ FETCH      get the real message        │             │
        │              │   ⑤ PARSE      sender / subject / headers   │             │
        │              │   ⑥ FILTER     keep / drop                  │   CleanEmail│
        │              │   ⑦ EXTRACT    decode body + attachments    │─────────────┘
        │              │   ⑧ EMIT       hand off the CleanEmail      │
        │              └───────────────────────────────────────────┘
        │                                   │
        │                                   ▼  (attachments)
        │                         📁 blob store (folder / S3)
```

---

## 3. The two phases

```
   ═══ PHASE 1 · SETUP (happens ONCE, when you call connect) ═══

      YOUR APP ──connect("gmail", credentials)──▶ mailflow
                                                    │
                                  ① OAuth refresh-token → access-token
                                  ② users.watch(mailbox, topic) → starting historyId
                                  ③ subscribe to Pub/Sub
                                                    │
                                            "Waiting for mail…"

   ═══ PHASE 2 · RUNTIME (repeats automatically, per email) ═══

      new email ─▶ Gmail ─push─▶ Pub/Sub ─▶ mailflow ─▶ fetch ─▶ filter ─▶ clean ─▶ YOUR APP
```

---

## 4. The 8 steps with the data at each step

```
   ① CONNECT      client_id + secret + refresh_token  ──▶  access_token (string, ~1h)

   ② WATCH        POST /users/{mbx}/watch             ──▶  { historyId: "464008" }  (cursor)

   ③ NOTIFY       Pub/Sub push                        ──▶  { emailAddress, historyId: 464081 }
                  (a POINTER — no email content)

   ④ FETCH        GET /history?startHistoryId=464008  ──▶  ["19ef7f50b0cf150c"]  (which msgs)
                  GET /messages/{id}?format=raw        ──▶  { raw: "<base64>", size: 46697 }

   ⑤ PARSE        raw RFC822                           ──▶  Envelope {from, subject, headers}

   ⑥ FILTER       Envelope                            ──▶  KEEP / DROP / UNCERTAIN

   ⑦ EXTRACT      raw RFC822                           ──▶  CleanEmail (body + attachments)

   ⑧ EMIT         CleanEmail                           ──▶  your stream() / on_email
```

```
   KEY:  Pub/Sub gives a POINTER (historyId)
         history.list gives WHICH messages (ids)
         messages.get gives the ACTUAL message (raw bytes)
         extract gives the CLEAN object (CleanEmail)
```

---

## 5. The filter flow (step ⑥ in detail)

```
   Envelope (sender / subject / headers)
        │
        ▼
   filter 1 ──┐
   filter 2 ──┤  each returns one of:
   filter 3 ──┤      KEEP   → accept, stop the chain
     …        │      DROP   → reject, stop the chain
        │     │      UNCERTAIN → no opinion, try the next
        ▼     ┘
   first KEEP or DROP wins  →  else the email passes through

   default filters = []  →  nothing dropped (safe-by-default)
```

```
   block a domain     → blacklist        allow ONLY a domain  → only_domain
   block a person     → block_sender     allow ONLY a person  → only_sender
   block personal     → no_personal      drop newsletters     → list_mail
   any custom rule    → a function (True = keep, False = drop)
```

---

## 6. Where the block happens (common doubt)

```
   Gmail ─push─▶ Pub/Sub ─▶ OUR APP ─fetch─▶ PARSE ─▶ FILTER ─DROP─▶ (discarded)
                  │                                        ▲
          delivers EVERYTHING                     the block happens HERE
          (no filtering)                          inside our pipeline,
                                                  after fetch, before save

   ✘ NOT blocked at Pub/Sub   ✘ NOT "saved but hidden"   ✔ fetched, then dropped in our code
```

---

## 7. Where the data is stored

```
   ┌─ emails.db  (SQLite DATABASE — rows) ───────────────────────────────┐
   │   subject · from · body_text · body_html · attachment POINTERS       │
   └──────────────────────────────────────────────────────────────────────┘
                                   │ storage_ref (a content-hash pointer)
                                   ▼
   ┌─ attachments/  (plain FOLDER — files) ──────────────────────────────┐
   │   9139224f…12fe48c   ← FILE = the actual bytes of the attachment      │
   │   4833a9fc…557005c   ← FILE = another attachment                      │
   └──────────────────────────────────────────────────────────────────────┘

   DOWNLOAD:  read pointer from DB  →  open attachments/<pointer>  →  stream the bytes back
   PRODUCTION: swap the folder for S3 / GCS — same pointer pattern
```

---

## 8. The components (what makes up mailflow)

```
   ┌──────────────── CORE (no vendor code) ────────────────┐
   │  pipeline · CleanEmail · ports · filters · stores      │
   └───────────────┬────────────────────────┬──────────────┘
         PROVIDERS  │                         │  EMITTERS / output
         ┌──────────┴─────────┐     ┌─────────┴──────────┐
         │ gmail   (live)     │     │ stream / on_email   │
         │ graph   (code-done)│     │ webhook / queue     │
         │ memory  (tests)    │     │ your app            │
         └────────────────────┘     └────────────────────┘
         STORES: cursor · dedupe · blob(attachments)
```

---

## 9. The 3 knobs the consumer controls

```
   connect("gmail", credentials, mailbox,
       filters=[ … ],   ──▶  WHICH emails reach you   (drop the rest)
       fields=[ … ],    ──▶  WHICH data you receive    (only what you use)
       stages=[ … ],    ──▶  PROCESS each email         (modify / drop)
   )
```

---

## 10. Reliability (runs in the background)

```
   ⏰ daily   → renew the watch        (so push never stops — watches expire ~7 days)
   ⏰ 15 min  → sweep / poll           (catch anything a push missed)
   on 404     → re-seed the cursor     (never stuck on a stale historyId)
   always     → atomic claim           (duplicates never double-processed)
   on failure → DLQ                    (one bad email never blocks the rest)
```

---

## The whole system in one diagram

```
   SETUP (once)                RUNTIME (per email, automatic)
   ────────────                ──────────────────────────────
   connect()                   new mail
      │                           │
      ├─ OAuth                    ▼
      ├─ watch          Gmail ─push─▶ Pub/Sub ─▶ ④ FETCH
      └─ subscribe                            │
                                              ▼
                                   ⑤ PARSE ─▶ ⑥ FILTER ─▶ ⑦ EXTRACT ─▶ ⑧ EMIT ─▶ YOUR APP
                                                 │                │
                                              (drop)         attachments ─▶ 📁 blob store
                                                                              │
                          ⏰ renew · sweep · 404-recover · dedupe · DLQ        emails.db
```
