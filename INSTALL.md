# Installing & using mailflow

`mailflow` connects to an inbox and hands your app a clean, provider-agnostic
`CleanEmail` object. **You store the email — the library never does.**

---

## 1. Install

You received a wheel file (`mailflow-0.1.0-py3-none-any.whl`). Python 3.12+.

```bash
# core only (light — just pydantic)
pip install mailflow-0.1.0-py3-none-any.whl

# with Gmail support (adds the Google SDKs)
pip install "mailflow-0.1.0-py3-none-any.whl[gmail]"
```

> On Windows PowerShell, the quotes around `"...whl[gmail]"` are required.

---

## 2. Smoke test (no setup, 5 seconds)

Confirm it imports and runs with the built-in in-memory provider:

```python
from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
raw = b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: it works\r\n\r\nhi"
mf = connect("memory", seed={stream: [SeedEmail("m1", raw)]}, tenant="acme")

for email in mf.stream():           # email is a CleanEmail
    print(email.subject, email.from_.address)
# -> it works a@partner.com
```

If that prints, the library is installed correctly.

---

## 3. Connect to real Gmail

### One-time Google setup
You need a Google Cloud project with: an OAuth client, a Pub/Sub topic + subscription,
and a refresh token for the mailbox. Full walkthrough: see `docs/google-setup-step-by-step.md`
(and the `scripts/get_gmail_refresh_token.py` helper). You end up with six values.

### Put secrets in the environment (never in code)
```bash
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
GMAIL_MAILBOXES=you@gmail.com
PUBSUB_PROJECT_ID=...
PUBSUB_TOPIC=gmail-notifications
PUBSUB_SUBSCRIPTION=mailflow
```

### Connect and receive mail
```python
import os
from mailflow import connect

mf = connect(
    "gmail",
    mailbox=os.environ["GMAIL_MAILBOXES"],
    credentials={
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret_ref": "env://GMAIL_CLIENT_SECRET",       # ref -> resolved at runtime
        "oauth_refresh_token_ref": "env://GMAIL_REFRESH_TOKEN",
        "project_id": os.environ["PUBSUB_PROJECT_ID"],
        "topic": os.environ["PUBSUB_TOPIC"],
        "subscription": os.environ["PUBSUB_SUBSCRIPTION"],
    },
    state="sqlite:///mf.db",        # optional: resume across restarts (see §5)
)

for email in mf.stream():           # blocks; waits for new mail
    my_app.save(email)              # YOUR database — the library ships none
```
Send an email to the watched mailbox → it arrives as a `CleanEmail` within seconds.

---

## 4. Three ways to receive emails (pick one)

```python
for email in mf.stream(): ...                       # pull loop
connect("gmail", ..., on_email=handle).run()        # push to a callback (blocking)
emails = connect("memory", seed=seed).fetch_new()   # batch: one pass -> list[CleanEmail]
```

---

## 5. State & deduplication (`state=`)

- `state="memory"` (default) — keeps nothing across restarts. On a restart you may
  re-receive recent mail, so **dedupe on `email.canonical_id`** (always present, stable):
  ```python
  if my_db.has(email.canonical_id):   # one-line dedupe
      continue
  ```
- `state="sqlite:///path/mf.db"` — persists the cursor + dedupe to one file; a restart
  resumes where it stopped. Pure stdlib `sqlite3`, no extra dependency.

---

## 6. The `CleanEmail` you get

```python
email.canonical_id     # always-present unique id (use for dedupe)
email.subject
email.from_.address    # parsed sender
email.to               # list[Recipient]
email.body_text        # decoded plain text
email.body_html
email.attachments      # list[Attachment] (bytes streamed to blob; ref only)
email.direction        # inbound | outbound
email.provider         # "gmail"
email.model_dump_json()   # serialize to JSON (it's a pydantic model)
```

The same shape comes out regardless of provider — that indifference is the point.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: google...` | install the extra: `pip install "mailflow-...whl[gmail]"` |
| `KeyError: 'env secret ... not set'` | the env var named in a `*_ref` isn't exported |
| No mail arrives | confirm the Google setup (watch + Pub/Sub) per `docs/google-setup-step-by-step.md` |
| PowerShell `&&` error | run commands one per line, or use `;` |
