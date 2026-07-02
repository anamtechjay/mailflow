# Google (Gmail + Pub/Sub) setup — step-by-step

Connect mailflow to Gmail so new mail flows into a Pub/Sub topic and through the
pipeline. Follow top to bottom for a new setup.

> **Big advantage over Microsoft:** Gmail API works with a **personal `@gmail.com`**
> via per-user OAuth consent — no Workspace/org needed. So you can test with your own
> Gmail. (Graph app-only needed an Exchange mailbox; Gmail does not.)

What you collect (all land in `.env`):
```
GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_MAILBOXES,
PUBSUB_PROJECT_ID, PUBSUB_TOPIC, PUBSUB_SUBSCRIPTION
```

---

## Step 1 — Google Cloud project  (→ `PUBSUB_PROJECT_ID`)
1. https://console.cloud.google.com → top project dropdown → **New Project** → name it
   (e.g. `mailflow`) → **Create**.
2. Copy the **Project ID** → `PUBSUB_PROJECT_ID`.

## Step 2 — Enable APIs
APIs & Services → **Enable APIs and services** → enable **Gmail API** and
**Cloud Pub/Sub API**.

## Step 3 — Pub/Sub topic + subscription  (→ `PUBSUB_TOPIC`, `PUBSUB_SUBSCRIPTION`)
1. Pub/Sub → **Topics → Create topic** → id `gmail-notifications` → Create.
   → `PUBSUB_TOPIC = gmail-notifications`.
2. Open the topic → **Create subscription** → id `mailflow`, **Delivery type: Pull** → Create.
   → `PUBSUB_SUBSCRIPTION = mailflow`.

## Step 4 — Let Gmail publish into your topic (the key link)
Topic → **Permissions / SHOW INFO PANEL → Add principal**:
- New principal: **`gmail-api-push@system.gserviceaccount.com`**
- Role: **Pub/Sub Publisher** → Save.
*(This is the exact analog of granting Graph's Change-Tracking SP "Data Sender".)*

## Step 5 — OAuth client  (→ `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`)
1. APIs & Services → **OAuth consent screen** → User type **External** → fill app name +
   your email → add scope `.../auth/gmail.readonly` → add your Gmail as a **Test user**.
2. **Credentials → Create credentials → OAuth client ID → Desktop app** → Create.
3. Copy **Client ID** → `GMAIL_CLIENT_ID`, **Client secret** → `GMAIL_CLIENT_SECRET`.

## Step 6 — Get a refresh token  (→ `GMAIL_REFRESH_TOKEN`)
Run the OAuth flow once (locally) to consent and obtain a refresh token. Quick way with
the installed `google-auth-oauthlib` (`pip install google-auth-oauthlib`):
```python
from google_auth_oauthlib.flow import InstalledAppFlow
flow = InstalledAppFlow.from_client_config(
    {"installed": {"client_id": "<ID>", "client_secret": "<SECRET>",
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token",
                   "redirect_uris": ["http://localhost"]}},
    scopes=["https://www.googleapis.com/auth/gmail.readonly"])
creds = flow.run_local_server(port=0)
print("REFRESH TOKEN:", creds.refresh_token)
```
Copy the printed token → `GMAIL_REFRESH_TOKEN`. → `GMAIL_MAILBOXES = your@gmail.com` (or `me`).

## Step 7 — Start the watch
`users.watch` registers the mailbox→topic notification (~7 days; renew daily). mailflow's
`GmailWatchManager.ensure_watch` does this; for a manual smoke test you can call the
Gmail API `users.watch` with `{topicName: projects/<proj>/topics/gmail-notifications,
labelIds:["INBOX"]}`.

---

## Fill `.env` and run
```powershell
pip install -e ".[gmail]"
Get-Content .env | Where-Object {$_ -match '=' -and -not $_.StartsWith('#')} | ForEach-Object { $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }
# then call run_service(...) (see README) OR test offline first:
python scripts/run_gmail_mock_emails.py
```

## Cost
Gmail API + Pub/Sub pull are **free at low volume** (generous free quotas) — typically
**$0** for a few mailboxes. No always-on resource like the Event Hubs namespace.

## Troubleshooting
| Symptom | Fix |
|---|---|
| Graph/Gmail can't read a personal mailbox | Gmail **can** (OAuth); Graph cannot (needs Exchange). |
| `users.watch` fails with permission error | Step 4 — grant `gmail-api-push@system.gserviceaccount.com` Publisher on the topic. |
| OAuth "access blocked / app not verified" | Add your Gmail as a **Test user** on the consent screen (Step 5). |
| 404 on `history.list` | historyId too old → mailflow full-resyncs; re-run `users.watch` to reseed. |
| No notifications arrive | Watch expired (7 days) — renew daily via `GmailWatchManager.renew_watch`. |
