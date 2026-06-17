# Gmail ingestion — DevOps handoff (what to provision + keys to return)

Provision the Google side for mailflow's Gmail provider, then return the keys in the
last table. Deliver all **SECRET** values via the secret manager (Vault/GSM/SSM) —
**not** email/chat. Reference: `docs/google-setup-step-by-step.md`.

## 1. Provisioning tasks (DevOps performs)
- [ ] Create / pick a **Google Cloud project**.
- [ ] Enable APIs: **Gmail API** + **Cloud Pub/Sub API**.
- [ ] Create **Pub/Sub topic**: `gmail-notifications`.
- [ ] Grant the Gmail push SA **`gmail-api-push@system.gserviceaccount.com`** the role
      **`roles/pubsub.publisher`** on that topic.  *(required — lets Gmail publish)*
- [ ] Create a **Pull subscription** on the topic: `mailflow`.
- [ ] **OAuth consent screen** (External): add scope
      `https://www.googleapis.com/auth/gmail.readonly`; add the mailbox owner as a **Test user**.
- [ ] Create an **OAuth client ID** (type: **Desktop app**) → gives client id + secret.
- [ ] Run the OAuth consent flow once for the mailbox → obtain a **refresh token**.
- [ ] Create a **consumer service account**, grant it **`roles/pubsub.subscriber`** on the
      `mailflow` subscription, and issue a **JSON key** (for the app to pull notifications).

## 2. Keys / values to return

| Key | Secret? | Where it comes from |
|---|:--:|---|
| `GMAIL_CLIENT_ID` | no | OAuth client ID |
| `GMAIL_CLIENT_SECRET` | **YES** | OAuth client |
| `GMAIL_REFRESH_TOKEN` | **YES** | OAuth consent flow (per mailbox) |
| `GMAIL_MAILBOXES` | no | the Gmail address to watch (e.g. `ops@…` or `me`) |
| `PUBSUB_PROJECT_ID` | no | the GCP project id |
| `PUBSUB_TOPIC` | no | `gmail-notifications` |
| `PUBSUB_SUBSCRIPTION` | no | `mailflow` |
| `GOOGLE_APPLICATION_CREDENTIALS` | **YES** (file) | path to the consumer SA JSON key (Pub/Sub pull auth) |

## 3. Notes for DevOps
- Two **separate** credentials: the OAuth refresh token authorizes **Gmail read**; the SA
  JSON key authorizes **Pub/Sub pull**. Both are required.
- Scope must be **`gmail.readonly`** (read-only). Do not grant write scopes.
- `gmail.readonly` is a **restricted scope** — fine with **test users**; using other
  people's mailboxes / publishing the app requires Google verification + a CASA security
  assessment.
- The Gmail **watch expires ~7 days** and is renewed daily by the app (no action needed).
- Cost: Gmail API + Pub/Sub are effectively **$0** at low volume.
