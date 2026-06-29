# mailflow — Complete Usage Guide

**One document: install → configure → get emails → filter → everything.**
mailflow connects to an inbox, throws away junk, and hands your app one clean,
provider-agnostic `CleanEmail`. You write ~15 lines; the library handles OAuth, Pub/Sub,
fetching, MIME parsing, attachments, dedup, and reliability.

---

## Contents
1. [Install](#1-install)
2. [Configuration (full reference)](#2-configuration-full-reference)
3. [Quickstart — get emails](#3-quickstart--get-emails)
4. [`connect()` — every parameter](#4-connect--every-parameter)
5. [Filtering (full detail)](#5-filtering-full-detail)
6. [Field selection — `fields`](#6-field-selection--fields)
7. [Stages — process each email](#7-stages--process-each-email)
8. [Three ways to receive](#8-three-ways-to-receive)
9. [Retrieve by message ID](#9-retrieve-by-message-id)
10. [The `CleanEmail` object](#10-the-cleanemail-object)
11. [State & persistence](#11-state--persistence)
12. [Reliability](#12-reliability)
13. [Full real-world examples](#13-full-real-world-examples)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Install

```bash
pip install mailflow                  # core only (light: pydantic)
pip install "mailflow[gmail]"         # + Gmail support (Google SDKs)
pip install "mailflow[graph]"         # + Outlook/Graph support (Azure SDKs)
```

Python 3.12+. On Windows PowerShell, quote the extra: `pip install "mailflow[gmail]"`.

**Files you create to use it:** just `app.py` (~15 lines) + `.env` (config). You write
none of the library.

---

## 2. Configuration (full reference)

### 2.1 Two kinds of values

| Kind | Examples | Where it lives |
|---|---|---|
| **Config** (not secret) | client_id, mailbox, project, topic | code / `.env` / a config file |
| **Secrets** (sensitive) | client_secret, refresh_token | referenced as `env://NAME`, resolved at runtime — **never in code** |

> Secrets are passed as **references** (`"env://GMAIL_CLIENT_SECRET"`), not literal values.
> The library resolves them via a `SecretProvider` at runtime. Default resolves `env://`
> from the environment; cloud stores (GSM/Key Vault) are pluggable.

### 2.2 Gmail configuration — the 7 values

```
# .env  (gitignored — never commit)
GMAIL_CLIENT_ID=1234-abc.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
GMAIL_MAILBOXES=you@company.com
PUBSUB_PROJECT_ID=your-gcp-project
PUBSUB_TOPIC=gmail-notifications
PUBSUB_SUBSCRIPTION=mailflow
# for the Pub/Sub subscriber auth:
GOOGLE_APPLICATION_CREDENTIALS=secrets/google-sa.json
```

| Value | What it is | Where to get it |
|---|---|---|
| `GMAIL_CLIENT_ID` | OAuth client id | Google Cloud Console → OAuth client |
| `GMAIL_CLIENT_SECRET` | OAuth client secret | same |
| `GMAIL_REFRESH_TOKEN` | per-mailbox consent token | `scripts/get_gmail_refresh_token.py` (one-time consent) |
| `GMAIL_MAILBOXES` | mailbox to watch | the address, or `me` |
| `PUBSUB_PROJECT_ID` | GCP project | Cloud Console |
| `PUBSUB_TOPIC` | Pub/Sub topic Gmail pushes to | Pub/Sub → Topics |
| `PUBSUB_SUBSCRIPTION` | subscription you consume | Pub/Sub → Subscriptions |

> One-time Google Cloud setup is in `docs/google-setup-step-by-step.md`. After that, it's
> the code below.

### 2.3 Load `.env` (the library does not auto-load it)

```python
# option A: python-dotenv (pip install python-dotenv)
from dotenv import load_dotenv; load_dotenv()

# option B: PowerShell, before running
# Get-Content .env | ? {$_ -match '=' -and -not $_.StartsWith('#')} | % { $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k.Trim(),$v.Trim()) }
```

---

## 3. Quickstart — get emails

### Zero-setup test (memory provider, no credentials)
```python
from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

raw = b"From: a@partner.com\r\nSubject: hello\r\n\r\nbody"
mf = connect("memory", seed={StreamRef(mailbox="me"): [SeedEmail("m1", raw)]})
for email in mf.stream():
    print(email.subject, email.from_.address)     # hello a@partner.com
```

### Live Gmail
```python
import os
from dotenv import load_dotenv
from mailflow import connect
load_dotenv()

mf = connect(
    "gmail",
    mailbox=os.environ["GMAIL_MAILBOXES"],
    credentials={
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret_ref": "env://GMAIL_CLIENT_SECRET",
        "oauth_refresh_token_ref": "env://GMAIL_REFRESH_TOKEN",
        "project_id": os.environ["PUBSUB_PROJECT_ID"],
        "topic": os.environ["PUBSUB_TOPIC"],
        "subscription": os.environ["PUBSUB_SUBSCRIPTION"],
    },
    state="sqlite:///mf.db",
)
for email in mf.stream():           # each new email arrives automatically
    my_app.save(email)
```

---

## 4. `connect()` — every parameter

```python
connect(
    provider,                 # "gmail" | "memory"   (graph via adapters.graph.live)
    *,
    credentials=None,         # dict of Gmail config (see §2.2)
    mailbox=None,             # which mailbox to watch
    seed=None,                # memory provider only: {StreamRef: [SeedEmail, ...]}
    state="memory",           # "memory" | "sqlite:///path.db"   (§11)
    filters=None,             # list of specs / functions / Filter objects   (§5)
    fields=None,              # list of field names to deliver   (§6)
    stages=None,              # list of post-processing functions   (§7)
    clean_fn=None,            # custom cleaning hook (runs first)
    on_email=None,            # callback: deliver to a function instead of a loop   (§8)
    secret_provider=None,     # custom secret resolver (default: env://)
    tenant="default",         # multi-tenant label
) -> Mailflow
```

Returns a `Mailflow` handle with `.stream()`, `.run()`, `.fetch_new()`, and the
message-ID helpers (§9).

---

## 5. Filtering (full detail)

`filters` is **one list**. Each entry is a built-in spec, a function, or a `Filter` object.
They run **in order**; the first KEEP or DROP wins. **Default `[]` = nothing filtered**
(safe — a reusable library never silently deletes mail).

### 5.1 Built-in filters

| `kind` | Effect | Params |
|---|---|---|
| `only_domain` | **keep ONLY these domains, drop all else** | `domains: [..]` |
| `only_sender` | **keep ONLY these exact people, drop all else** | `addresses: [..]` |
| `whitelist` | KEEP these domains (others still pass through) | `domains: [..]` |
| `blacklist` | DROP these domains | `domains: [..]` |
| `no_personal` | DROP consumer domains (gmail/yahoo/hotmail/…) | `domains: [..]` (optional) |
| `internal_domain` | DROP your own org's domain | `domains: [..]` |
| `subject` | DROP if subject matches a regex | `patterns: [..]` |
| `list_mail` | DROP newsletters / auto-replies (List-Id / Auto-Submitted) | none |

```python
filters=[
    {"kind": "blacklist", "domains": ["spam.com"]},
    {"kind": "no_personal"},
    {"kind": "only_domain", "domains": ["company.com"]},
]
```

### 5.2 ⚠️ `whitelist` vs `only_domain` (the common mistake)

```
  whitelist    → KEEPS matches, but lets OTHERS through.  NOT "only this domain".
  only_domain  → keeps your domain, DROPS everything else. ← use this for "only X".
```

### 5.3 Custom function filter

```python
def my_rule(env) -> bool:
    # env: from_, subject, to, list_id, headers …
    return env.from_.address.endswith("@company.com")   # True = keep, False = drop

filters=[my_rule]                       # or an inline lambda
filters=[lambda env: "invoice" in env.subject.lower()]
```

`True` passes the email to the next filter; `False` drops it. The function receives the
**Envelope** (cheap, pre-extraction): sender, subject, recipients, headers.

### 5.4 Common "I only want X" recipes

```python
# only one domain
filters=[{"kind": "only_domain", "domains": ["techjays.com"]}]
# only a few people
filters=[{"kind": "only_sender", "addresses": ["billing@vendor.com", "ap@client.com"]}]
# only mail addressed to support@
filters=[lambda env: any(r.address == "support@acme.com" for r in env.to)]
# only invoices, exclude newsletters
filters=[{"kind": "subject", "patterns": ["(?i)invoice"]}, {"kind": "list_mail"}]
```

---

## 6. Field selection — `fields`

Declare the data you want; receive only that (a dict). Default = the full `CleanEmail`.

```python
mf = connect("gmail", ..., fields=["subject", "from", "attachments"])
for email in mf.stream():
    # email == {"subject": "...", "from": <Recipient>, "attachments": [...]}
```

- Valid names = `CleanEmail` fields (`"from"` is an alias for `from_`).
- Unknown name → `ValueError`. Empty list → `{}`.
- Why: smaller payload, privacy (skip the body if you only need metadata), self-documenting.

---

## 7. Stages — process each email

`stages` run on the full `CleanEmail` after extraction. Each returns the email (continue),
a modified email, or a falsy value (drop).

```python
def enrich(email):
    return email.model_copy(update={"subject": "[seen] " + email.subject})

def only_pdfs(email):
    return email if any(a.content_type == "application/pdf" for a in email.attachments) else None

mf = connect("gmail", ..., stages=[enrich, only_pdfs])
```

`clean_fn=fn` runs as the first stage (a custom cleaning hook).

**Filter vs stage:** sender/subject/headers → use a **filter** (cheap, pre-extraction);
body/attachments/size → use a **stage** (needs the decoded email).

---

## 8. Three ways to receive

```python
# A) pull loop
for email in mf.stream():
    handle(email)

# B) callback (the library calls you)
mf = connect("gmail", ..., on_email=lambda e: my_app.save(e))
mf.run()                              # blocks

# C) batch (one pass, returns a list) — memory/batch use
emails = mf.fetch_new()
```

---

## 9. Retrieve by message ID

Pull any part on demand — no need to store emails.

```python
email = mf.get_email(message_id)        # -> CleanEmail
mf.get_body(message_id)                 # -> str
mf.get_recipients(message_id)           # -> list[Recipient]
mf.get_attachments(message_id)          # -> list[Attachment]
```

---

## 10. The `CleanEmail` object

```python
email.canonical_id        # always-present unique id (use for dedup)
email.message_id          # RFC Message-ID (nullable)
email.provider            # "gmail" | "graph"
email.provider_message_id # provider's own id (for re-fetch)
email.direction           # inbound | outbound | unknown
email.from_.address       # parsed sender   (name + address)
email.sender, email.reply_to
email.to / cc / bcc       # list[Recipient]
email.subject
email.date_utc, email.received_at
email.body_text           # decoded plain text
email.body_html
email.attachments         # list[Attachment]: filename, content_type, size_bytes,
                          #   content_hash (sha256), is_inline, storage_ref (pointer, NOT bytes)
email.labels / categories / folder
email.list_id, email.list_unsubscribe, email.auto_submitted
email.raw_headers         # full original header map (escape hatch)
email.model_dump_json()   # serialize (pydantic model)
```

Same shape from any provider. **Attachments** are streamed to a blob store; the email
carries a `storage_ref` pointer, not the bytes (keeps events small).

---

## 11. State & persistence

```
  state="memory"              (default) — stateless; on restart you may re-receive recent
                              mail, so DEDUP on email.canonical_id (one line in your store).
  state="sqlite:///mf.db"     — persists cursor + dedup; a restart resumes where it stopped.
```

The library stores **only a tiny cursor/dedup bookmark** — never your emails. Where the
email goes is your app's decision.

---

## 12. Reliability

A long-running Gmail service stays healthy automatically:

- **Watch renewal** — renews daily so notifications never stop (`watch_renew_seconds`).
- **Stale-cursor recovery** — re-seeds on a too-old historyId so the mailbox never sticks.
- **Sweep** — periodic poll catches anything a push missed (`sweep_seconds`).
- **Exactly-once processing** — an atomic claim means duplicates are never double-processed.
- **DLQ** — poison/oversized mail is dead-lettered with a reason; one bad email never blocks
  the rest.

---

## 13. Full real-world examples

```python
# Support desk: only mail to support@, no newsletters
connect("gmail", mailbox="support@acme.com", credentials={...},
    filters=[lambda env: any(r.address == "support@acme.com" for r in env.to),
             {"kind": "list_mail"}])

# Document pipeline: only PDFs from a partner domain, deliver minimal data
connect("gmail", mailbox=..., credentials={...},
    filters=[{"kind": "only_domain", "domains": ["partner.com"]}],
    fields=["subject", "from", "attachments"],
    stages=[lambda e: e if any(a.content_type=="application/pdf" for a in e.attachments) else None])

# Invoice bot: only known senders + invoice subjects, callback style
def handle(email): book(email)
connect("gmail", mailbox=..., credentials={...},
    filters=[{"kind": "only_sender", "addresses": ["billing@vendor.com"]},
             {"kind": "subject", "patterns": ["(?i)invoice"]}],
    on_email=handle).run()
```

---

## 14. Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: google…` | install the extra: `pip install "mailflow[gmail]"` |
| `KeyError: env secret … not set` | the env var named in a `*_ref` isn't loaded (run the `.env` loader) |
| No mail arrives | check the Google setup (watch + Pub/Sub publisher grant) in `docs/google-setup-step-by-step.md` |
| `whitelist` didn't exclude others | use `only_domain` / `only_sender` — `whitelist` only *keeps* matches |
| Re-processing on restart | use `state="sqlite:///mf.db"`, or dedup on `canonical_id` |
| PowerShell `&&` error | run commands one per line, or use `;` |

---

*One contract: `CleanEmail` at `SCHEMA_VERSION = "1.0"`. Same object from Gmail or Outlook —
write your app once.*
