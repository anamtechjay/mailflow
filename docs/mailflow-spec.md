# mailflow — Living Technical Spec

> **This is the source of truth.** Update this spec FIRST, then change the code — not the
> other way round. Anyone (technical) should be able to read this and understand how the
> library works without reading all the code.
>
> **Status:** living. Last updated 2026-06-25.

---

## 1. What the library does (the black box)

A developer gives configuration and registers a function (or a loop). The library connects
to the inbox, receives mail automatically, filters it, cleans it, and hands the app a
`CleanEmail` — the app never touches Gmail/Graph APIs.

```
   app: connect(...) + a function   →   library does ALL email work   →   CleanEmail
```

The app's whole mental model is three things: **`connect()`**, **`filters=[...]`**, and
**`mf.stream()` or `on_email`**. Everything else is hidden.

---

## 2. How receiving works (summary)

Full walkthrough: `docs/mailflow-call-explanation.md`. In short:

```
  Gmail push → Pub/Sub notification {emailAddress, historyId}   (a POINTER, no content)
     → library fetches the changed message(s) from the Gmail API
     → [pipeline: claim → size → parse → FILTER → extract] → CleanEmail
     → your function / loop
```

The notification carries no email content — only a watermark. The library fetches the real
message, then runs it through the pipeline.

---

## 3. Filter configuration (the design)

### 3.1 The one rule: `filters` is a list

`connect(..., filters=[...])` takes a single list. Each entry is one of three kinds:

```python
filters = [
    {"kind": "blacklist", "domains": ["spam.com"]},   # 1. a built-in spec (dict)
    {"kind": "no_personal"},                          #    (no params needed)
    lambda env: "invoice" in env.subject.lower(),     # 2. a custom function
    BlacklistFilter(domains={"x.com"}),               # 3. a Filter object (advanced)
]
```

Filters run **in order**; the first KEEP or DROP wins; otherwise the email passes.

### 3.2 Built-in filters

| kind | drops/keeps | params |
|---|---|---|
| `whitelist` | KEEP if sender domain in list (others still pass) | `domains: [..]` |
| `only_domain` | **Keep ONLY these domains — DROP all else** | `domains: [..]` |
| `only_sender` | **Keep ONLY these exact people — DROP all else** | `addresses: [..]` |
| `blacklist` | DROP if sender domain in list | `domains: [..]` |
| `internal_domain` | DROP if sender is your own domain | `domains: [..]` |
| `subject` | DROP if subject matches a regex | `patterns: [..]` |
| `list_mail` | DROP newsletters/auto-mail (List-Id / Auto-Submitted) | none |
| `no_personal` | DROP consumer domains (gmail/yahoo/hotmail/…) | `domains: [..]` (optional override) |

> **`whitelist` vs `only_domain`/`only_sender`:** `whitelist` *keeps* matches but lets
> others through (UNCERTAIN). `only_domain`/`only_sender` keep ONLY the listed
> domains/people and **drop everything else** — use these when you want an exclusive slice.

### 3.2a What slice does a consumer actually want? (selection needs)

Different projects want a different slice of the inbox. The library should answer each.
**Key rule: which SIGNAL the rule uses decides whether it is a FILTER or a STAGE.**

```
  FILTER (cheap, runs on the Envelope BEFORE extraction):
    sender · recipient · subject · headers   →  decide before downloading the body

  STAGE (rich, runs on the full CleanEmail AFTER extraction):
    body content · attachments · size        →  needs the decoded message
```

| The consumer wants… | How to do it | filter or stage? | status |
|---|---|---|---|
| Only ONE domain (`@techjays.com`) | `only_domain` | filter | ✅ built |
| Only ONE person (`alice@x.com`) | `only_sender` | filter | ✅ built |
| Only a few specific people | `only_sender` (list of addresses) | filter | ✅ built |
| Block one domain / sender | `blacklist` | filter | ✅ built |
| Block personal/consumer mail | `no_personal` | filter | ✅ built |
| Exclude our own org | `internal_domain` | filter | ✅ built |
| Only certain subjects (e.g. "invoice") | `subject` regex | filter | ✅ built |
| Exclude newsletters / auto-replies | `list_mail` | filter | ✅ built |
| Any custom sender/subject rule | a **function** `fn(env)->bool` | filter | ✅ built |
| Only mail addressed TO `support@` | a function on `env.to` | filter | ✅ via function |
| Only mail WITH attachments | a **stage** checking `email.attachments` | stage | ✅ via stage |
| Only a certain attachment type (PDF) | a stage on `email.attachments[].content_type` | stage | ✅ via stage |
| Only body containing a keyword | a stage on `email.body_text` | stage | ✅ via stage |
| Only inbound (or only outbound) | a stage on `email.direction` | stage | ✅ via stage |
| Only small / large emails | a stage on `email.message_size_bytes` | stage | ✅ via stage |
| Future dedicated built-ins (has_attachment, only_to) | add a built-in `kind` | either | 💡 easy to add |

Examples for the stage-based ones (no new code needed — just a function):

```python
# only emails that have attachments
stages=[lambda e: e if e.attachments else None]

# only PDFs
stages=[lambda e: e if any(a.content_type == "application/pdf" for a in e.attachments) else None]

# only mail addressed to support@ (filter, on the Envelope)
filters=[lambda env: any(r.address == "support@acme.com" for r in env.to)]

# only body mentioning "urgent"
stages=[lambda e: e if "urgent" in e.body_text.lower() else None]
```

### 3.3 Custom function contract

```python
def my_filter(env) -> bool:
    # env is the Envelope: env.from_.address, env.subject, env.to, env.list_id, env.headers
    return True    # True  = pass to the next filter
                   # False = DROP this email
```

The function receives the **Envelope** (cheap, pre-extraction — sender/subject/headers),
NOT the full body. (Body-level logic belongs in a **stage**, §5.) `True` passes on; `False`
drops.

### 3.4 Safe-by-default (non-negotiable)

`filters` defaults to `[]` → **nothing is filtered.** A reusable library must never silently
delete a project's real mail. Each app opts into destructive rules explicitly.

### 3.5 Internally

Every entry compiles to a `Filter` (`name` + `evaluate(env, ctx) -> keep/drop/uncertain`)
and runs in the existing `FilterChain`. Dict → `build_filter(kind, params)`; function →
`FunctionFilter`; object → as-is.

---

## 4. Retrieve by message ID (lazy API)

Sometimes you have a message ID and want to pull parts on demand (Dharma: "if retrieved by
message ID, the storage problem is solved"). The handle exposes:

```python
email = mf.get_email(message_id)          # -> CleanEmail (fetch raw + extract on demand)
mf.get_body(message_id)                   # -> str
mf.get_recipients(message_id)             # -> list[Recipient]
mf.get_attachments(message_id)            # -> list[Attachment]
```

For Gmail this fetches `messages.get(format=raw)` and runs the `MimeExtractor`. So the app
never stores emails — it can re-fetch any part anytime from the message ID.
(Memory provider raises a clear error — there is nothing to fetch.)

---

## 4a. Field selection — `fields=[...]` (declare the data you want)

Not every consumer wants the full `CleanEmail`. Instead of preset output modes, the
consumer **declares the field names they need** and receives only those — the SQL-`SELECT`
/ GraphQL / Gmail-`fields=` pattern.

```python
mf = connect("gmail", credentials=..., fields=["subject", "from", "attachments"])

for email in mf.stream():
    # email is a dict with ONLY those keys:
    #   {"subject": "...", "from": <Recipient>, "attachments": [<Attachment>, ...]}
    ...
```

- **Default `fields=None`** → the full `CleanEmail` (unchanged).
- Valid names are the `CleanEmail` fields; **`"from"` is accepted as an alias** for `from_`
  (output key stays `"from"`). An unknown name raises a clear `ValueError`.
- `fields=[]` → `{}` (explicit "no data").
- Works for all delivery shapes: `stream()`, `fetch_new()`, and `on_email`.

It pairs with the other knobs — three clean choices:

```
  filters=[...]  → WHICH emails reach me    (drop the rest)
  fields=[...]   → WHICH data I receive       (drop the fields I don't need)
  stages=[...]   → PROCESS each email          (modify or drop)
```

**Why declare fields (not a full object):** smaller payload to a webhook/queue, privacy
(don't deliver the body if you only need metadata), and the config self-documents exactly
what data the app uses. Projection happens at delivery — the core pipeline is untouched.

## 5. Pipeline stages (post-processing)

`connect(..., stages=[fn1, fn2])`. Each stage runs on the final `CleanEmail`:

```python
def stage(email):                 # email is a CleanEmail (full body + attachments)
    ...                           # return the email (or a modified one) to continue
    return email                  # return None / False to STOP (drop this email)
```

Stages run **after** extraction, **between** the pipeline and your `stream()/on_email`.
A stage returning a falsy value halts that email. This is the composable "stage 1 → stage 2
→ stage 3" Dharma described — each stage can transform or filter.

```
  CleanEmail → clean_fn → stage1 → stage2 → ... → your function
                  │          │
              (falsy = drop at any stage)
```

---

## 6. Custom clean function

`connect(..., clean_fn=fn)` where `fn(email: CleanEmail) -> CleanEmail`. Runs as the FIRST
stage — lets the app tweak/augment the default cleaning (e.g. strip signatures, add fields)
without replacing the whole extractor.

---

## 7. The unified `connect()` (one front door)

```python
mf = connect(
    "gmail",
    credentials={...}, mailbox="me",
    state="sqlite:///mf.db",          # optional: restart-safe
    filters=[                         # §3
        {"kind": "blacklist", "domains": ["spam.com"]},
        {"kind": "no_personal"},
        lambda env: "invoice" in env.subject.lower(),
    ],
    stages=[enrich, route],           # §5
    clean_fn=my_clean,                # §6
    on_email=handle,                  # or use mf.stream()
)
```

---

## 8. How attachments work (clarifies the call discussion)

mailflow fetches Gmail with **`format=raw`**, so the FULL message — including the attachment
(base64) — arrives inline in the raw RFC822 payload. The `MimeExtractor` decodes it and
streams the bytes to the blob store, keeping only a `storage_ref` pointer on the CleanEmail.

> Note: Gmail also offers `format=full` + a separate `attachments.get(attachmentId)` call
> (the "fetch by message ID" path). mailflow uses `format=raw` (inline), and
> `mf.get_attachments(message_id)` re-fetches on demand for the lazy API (§4).

---

## 8a. Gmail reliability (keep the long-running service alive)

Three mechanisms keep a continuously-running Gmail service healthy. All are additive
(no core change) and configurable on `GmailConfig` (0 disables a feature):

```
  WATCH RENEWAL   (watch_renew_seconds, default 86400 = daily)
     Gmail watches expire in ~7 days. A background timer re-calls users.watch so
     notifications never silently stop. (renew does NOT move the cursor.)

  STALE-historyId RECOVERY  (automatic)
     If history.list returns 404 (the stored historyId is too old to diff from),
     the provider re-seeds the cursor to the CURRENT historyId (via getProfile) and
     continues — the mailbox is never stuck. Tradeoff: the gap mail is skipped (safe
     recovery; stuck-forever is worse). The sweep narrows the window.

  SWEEP / poll safety net  (sweep_seconds, default 900 = every 15 min)
     "Push = a hint, poll = the truth." A periodic delta-poll per mailbox diffs from
     the stored cursor and runs the pipeline — catching anything a push missed.
     Idempotent: the dedup claim + monotonic cursor make overlap with push harmless.
```

Both timers run on background daemon threads via `IntervalScheduler` while the Pub/Sub
consume loop blocks the main thread; they stop when the loop exits.

## 9. What's pending (honesty — don't assume we're close)

```
  ✅ DONE:      Gmail receive flow, connect(), CleanEmail, persistent SQLite stores,
                built-in filters (whitelist/blacklist/internal/subject/list_mail/no_personal),
                unified filters=[...] wired into connect() (specs + functions + objects),
                custom-function filters, only_domain/only_sender, message-ID retrieval
                (get_email/body/recipients/attachments), pipeline stages + custom clean_fn,
                field selection (fields=[...] projection)
                Gmail reliability: watch renewal scheduling, stale-historyId (404)
                recovery, and the sweep/poll safety net
  🟡 PARTIAL:   live demo scripts still use memory stores (use state="sqlite://" for safety)
  ❌ PENDING:   observability export (metrics/traces), dead-man's-switch alerting,
                reference deploy, edge-case hardening (malformed MIME, encodings, huge
                bodies), Microsoft (parked)
```

This spec is updated as each item lands. Sections 3–6 are written BEFORE their code (per the
"spec first" rule) and reconciled to "as-built" when implemented.

---

## 10. Design decisions (the "why")

- **Filters take the Envelope, stages take the CleanEmail** — filter cheaply before the
  expensive extract; process richly after.
- **Custom filter returns bool** (`True`=pass / `False`=drop) — simplest possible contract.
- **One `filters` list, not many config styles** — one mental model; specs for built-ins,
  functions for anything else; it's plain data (serializable, testable).
- **Additive hooks, no core change** — stages/clean wrap the output; `core/pipeline.py`
  stays untouched, so the proven engine is unchanged.
- **Lazy `get_email(message_id)`** — the app needn't store emails; re-fetch any part anytime.
