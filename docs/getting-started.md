# Integrate mailflow in ~15 lines

**mailflow** connects to an inbox, filters the junk, and hands your app one clean,
provider-agnostic `CleanEmail` — so you never touch Gmail's API, OAuth, Pub/Sub, MIME
parsing, dedup, or reliability. You write a few lines; the library does the rest.

> **The whole integration is 2 files you write:** `app.py` (~10–15 lines) and a 7-line
> `.env`. The library is installed — you write none of it.

---

## 1. Install

```bash
pip install mailflow                      # core (light: just pydantic)
pip install "mailflow[gmail]"             # + Gmail support
```

Python 3.12+. On Windows PowerShell, quote the extra: `pip install "mailflow[gmail]"`.

---

## 2. Try it in 6 lines (no credentials, no setup)

```python
from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

raw = b"From: a@partner.com\r\nSubject: hello\r\n\r\nbody"
mf = connect("memory", seed={StreamRef(mailbox="me"): [SeedEmail("m1", raw)]})
for email in mf.stream():
    print(email.subject, email.from_.address)     # -> hello a@partner.com
```

If that prints, you're installed and working. Now connect to real mail.

---

## 3. Connect to real Gmail (~15 lines)

**`.env`** (7 values — secrets stay here, never in code):

```
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
GMAIL_MAILBOXES=you@company.com
PUBSUB_PROJECT_ID=...
PUBSUB_TOPIC=gmail-notifications
PUBSUB_SUBSCRIPTION=mailflow
```

**`app.py`:**

```python
import os
from mailflow import connect

mf = connect(
    "gmail",
    mailbox=os.environ["GMAIL_MAILBOXES"],
    credentials={
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret_ref": "env://GMAIL_CLIENT_SECRET",      # a reference, resolved at runtime
        "oauth_refresh_token_ref": "env://GMAIL_REFRESH_TOKEN",
        "project_id": os.environ["PUBSUB_PROJECT_ID"],
        "topic": os.environ["PUBSUB_TOPIC"],
        "subscription": os.environ["PUBSUB_SUBSCRIPTION"],
    },
    state="sqlite:///mf.db",        # optional: survive restarts
)

for email in mf.stream():           # blocks; each new email arrives automatically
    my_app.save(email)              # ← your logic
```

Send a mail to the watched mailbox → it arrives as a `CleanEmail` within seconds.

---

## 4. Pick exactly what you want

Add these as arguments to the same `connect(...)` — no extra files:

```python
    # which emails reach you (built-in specs OR your own function)
    filters=[
        {"kind": "only_domain", "domains": ["company.com"]},   # only this domain
        {"kind": "no_personal"},                                # block gmail/yahoo/…
        lambda env: "invoice" in env.subject.lower(),           # any custom rule
    ],

    # which data you receive (smaller payload, just what you use)
    fields=["subject", "from", "attachments"],

    # process each email (modify or drop)
    stages=[lambda e: e if e.attachments else None],            # e.g. only ones with files
```

Three knobs: **`filters`** = which emails · **`fields`** = which data · **`stages`** = process each.

---

## 5. Two ways to receive

```python
# A) pull loop
for email in mf.stream():
    handle(email)

# B) callback (the library calls you)
def handle(email):
    my_app.save(email)
mf = connect("gmail", ..., on_email=handle)
mf.run()
```

---

## 6. Pull any part on demand (by message ID)

```python
email = mf.get_email(message_id)        # -> CleanEmail
mf.get_body(message_id)                 # -> str
mf.get_attachments(message_id)          # -> list[Attachment]
```

---

## 7. What you get — the `CleanEmail`

```python
email.canonical_id        # always-present unique id (use for dedup)
email.subject
email.from_.address       # parsed sender
email.to / cc / bcc       # list[Recipient]
email.body_text           # decoded plain text
email.body_html
email.attachments         # list[Attachment] — bytes streamed to storage, ref only
email.direction           # inbound | outbound
email.provider            # "gmail"
email.model_dump_json()   # serialize (it's a pydantic model)
```

Same shape from any provider — that indifference is the point.

---

## 8. The 4 kinds of code you write (that's all)

```
  1. IMPORT     from mailflow import connect
  2. CONNECT    mf = connect("gmail", credentials=…)
  3. RECEIVE    for email in mf.stream():   (or  on_email=handle)
  4. YOUR LOGIC my_app.save(email)
```

Everything else — OAuth refresh, the Pub/Sub watch + consume, fetching, MIME decoding,
attachments, dedup, the watch renewal, stale-cursor recovery, and the safety-net sweep —
lives **inside the installed library.**

---

## Line-count summary

| What you want | `app.py` | `.env` | Total you write |
|---|---|---|---|
| Try it (memory) | ~6 | 0 | **~6 lines** |
| Live Gmail | ~15 | 7 | **~22 lines** |
| + filters + fields | ~18 | 7 | **~25 lines** |

---

## Reliability (you get it for free)

A long-running service stays healthy automatically:

- **Watch renewal** — renews daily so notifications never stop (Gmail watches expire ~7 days).
- **Stale-cursor recovery** — re-seeds on a too-old historyId so the mailbox never gets stuck.
- **Sweep** — periodically polls to catch anything a push notification missed.
- **At-least-once delivery** — an atomic claim de-dupes within the system, but duplicates can
  still occur, so every `EmailEvent` carries an `idempotency_key`
  (`tenant|mailbox|provider_message_id`) for consumer-side dedupe.

(Tune or disable via `watch_renew_seconds` / `sweep_seconds`.)

---

## FAQ

**Do I need to set up Google Cloud?** Once: an OAuth client + a Pub/Sub topic/subscription +
a refresh token. See `docs/google-setup-step-by-step.md`. After that, it's the ~15 lines above.

**Where do attachments go?** Streamed to a blob store (local folder by default, or your
S3/GCS); the `CleanEmail` carries a `storage_ref` pointer, not the bytes.

**Does the library store my emails?** No. It delivers a `CleanEmail` and you store it however
you like. The library keeps only a tiny cursor/dedup bookmark.

**Outlook?** The Graph adapter exists (same `CleanEmail` contract); it needs a real Exchange
mailbox to run live.
