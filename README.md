# mailflow (core spine)

Provider- and transport-agnostic email ingestion. This package is the **core**:
the full pipeline (parse → filter → extract → emit) plus the §8 correctness
invariants, runnable end-to-end against an in-memory provider with zero setup.

## Quickstart (zero external setup)

```python
from mailflow import build_from_config, MailflowConfig
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

cfg = MailflowConfig.model_validate({
    "tenant": "acme",
    "provider": {"kind": "memory"},
    "filters": [{"kind": "blacklist", "params": {"domains": ["spam.com"]}}],
    "emitter": {"kind": "memory"},
})
stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
seed = {stream: [SeedEmail("m1", b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: hi\r\n\r\nhello")]}

pipe = build_from_config(cfg, seed=seed)
report = pipe.run_once()
print(report.emitted, report.dropped, report.dead_lettered)
```

## Use it as a library — `connect()`

One front door. You bring credentials; the library hands you a `CleanEmail` and stays out
of your way. **You store the email — the library never does.** It keeps only a tiny
cursor/dedupe bookmark (in memory by default; on disk if you ask).

```python
from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

# zero-setup, in-memory (great for trying it / tests)
stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
raw = b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: hi\r\n\r\nhello"
mf = connect("memory", seed={stream: [SeedEmail("m1", raw)]}, tenant="acme")

for email in mf.stream():          # email is a CleanEmail
    print(email.subject, email.from_.address)
```

Live Gmail (needs the `gmail` extra + credentials). The same `CleanEmail` comes out:

```python
mf = connect(
    "gmail",
    credentials={
        "client_id": "<oauth-client-id>",
        "client_secret_ref": "env://GMAIL_CLIENT_SECRET",
        "oauth_refresh_token_ref": "env://GMAIL_REFRESH_TOKEN",
        "project_id": "<gcp-project>", "topic": "gmail-notifications", "subscription": "mailflow",
    },
    mailbox="me",
    state="sqlite:///mf.db",       # opt-in: remember progress across restarts
)
for email in mf.stream():
    my_app.save(email)             # YOUR database — the library ships none
```

Three ways to receive, pick one per handle:

```python
for email in mf.stream(): ...                       # pull loop
connect("gmail", ..., on_email=handle).run()        # push to a callback (blocking)
emails = connect("memory", seed=seed).fetch_new()   # batch: one pass -> list[CleanEmail]
```

**State (`state=`):**
- `"memory"` (default) — stateless; on restart you may re-receive recent mail, so **dedupe
  on `email.canonical_id`** (always present, stable).
- `"sqlite:///path/mf.db"` — persists cursor + dedupe to one file; a restart resumes where
  it stopped. Pure stdlib `sqlite3`, no extra dependency.

### Running tests / types (this checkout, Windows)

```bash
python -m pytest      # unit suite (no cloud needed)
python -m mypy        # strict
```

## What's here vs later

| Plan | Scope |
|------|-------|
| **1 (this repo)** | core + in-memory adapters |
| 2 | Microsoft Graph adapter + webhook/subscribe |
| 3 | Gmail adapter |
| 4 | reference deploy, allowlisted plugins, attachment streaming, LLM classifier |

The wire contract is `EmailEvent` / `CleanEmail` at `SCHEMA_VERSION = "1.0"`.
Adapters plug into the ports in `mailflow.core.ports` without touching `core`.

## Live: Outlook via Microsoft Graph + Azure Event Hubs

The Graph adapter (`mailflow.adapters.graph`) ingests Outlook/Exchange mail app-only,
with change notifications delivered **directly to Azure Event Hubs (no public webhook)**.
The unit suite needs none of this; install the extra only to run live:

```bash
pip install -e ".[graph]"
```

Provision Azure + Entra first (one-time) per
`docs/superpowers/plans/2026-06-11-microsoft-graph-eventhubs-provisioning.md`, then run:

```python
from mailflow.adapters.graph import GraphConfig, EventHubConfig
from mailflow.adapters.graph.live import run_service
from mailflow.secrets import EnvSecretProvider
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

graph_cfg = GraphConfig.model_validate({
    "tenant_id": "<tenant-guid>", "client_id": "<app-guid>",
    "client_secret_ref": "env://GRAPH_CLIENT_SECRET",   # resolved at runtime, never hardcoded
    "mailboxes": ["ops@acme.com"],
})
eh = EventHubConfig(namespace="evh-mailflow", hub="graph-notifications", tenant_domain="acme.com")

run_service(                       # blocks, consuming the hub
    graph_cfg=graph_cfg, eventhub=eh, tenant="acme",
    secret_provider=EnvSecretProvider(),
    emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
    cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
    blob_store=InMemoryBlobStore(),
    connection_string="<eventhub-connection-string>",     # OR credential=DefaultAzureCredential()
    checkpoint_connection_string="<storage-conn>", checkpoint_container="eh-checkpoints",
)
```

You supply the credentials/keys; nothing is hardcoded. Auth is pluggable — pass a
`connection_string` (SAS) for a quick dev run, or `credential=DefaultAzureCredential()`
for the RBAC path. Subscriptions are created/renewed by `SubscriptionReconciler`;
lifecycle events (`reauthorizationRequired`/`subscriptionRemoved`/`missed`) are handled
by `GraphLifecycleHandler`; `GraphProvider.sweep` delta-replays a folder on catch-up.
See `docs/superpowers/plans/2026-06-15-mailflow-graph-eventhubs-live-flow.md`.

## Live: Gmail via Pub/Sub

The Gmail adapter (`mailflow.adapters.gmail`) ingests Gmail app-via-OAuth, with change
notifications delivered to **Google Cloud Pub/Sub**. Unlike Microsoft (app-only needs an
Exchange mailbox), Gmail works with a **personal `@gmail.com`** via OAuth consent. Gmail
returns raw RFC822, so the adapter **reuses the core `MimeEnvelopeParser` + `MimeExtractor`**.

```bash
pip install -e ".[gmail]"
```

Provision per `docs/google-setup-step-by-step.md`, then:

```python
from mailflow.adapters.gmail import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.live import run_service
from mailflow.secrets import EnvSecretProvider
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

gmail_cfg = GmailConfig.model_validate({
    "client_id": "<oauth-client-id>",
    "client_secret_ref": "env://GMAIL_CLIENT_SECRET",
    "oauth_refresh_token_ref": "env://GMAIL_REFRESH_TOKEN",
    "mailboxes": ["you@gmail.com"],          # or "me"
})
ps = PubSubConfig(project_id="<gcp-project>", topic="gmail-notifications", subscription="mailflow")

run_service(                       # blocks, consuming the Pub/Sub subscription
    gmail_cfg=gmail_cfg, pubsub_cfg=ps, tenant="me",
    secret_provider=EnvSecretProvider(),
    emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
    cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
    blob_store=InMemoryBlobStore(),
)
```

Offline demo (no Google account): `python scripts/run_gmail_mock_emails.py`.
The cursor is the Gmail `historyId`; `GmailWatchManager` renews the watch daily; the
Pub/Sub ack is the checkpoint. See
`docs/superpowers/plans/2026-06-16-mailflow-gmail-adapter.md`.

### Tracking the free $200 Azure credit

The raw "credit remaining" balance isn't exposed by API for trial accounts (portal
only), and the trial credit **expires 30 days after signup regardless of usage**. So
the best guardrail is a native Azure Budget alert (set once):

```bash
az consumption budget create --budget-name mailflow-200 --amount 200 \
  --time-grain Monthly --category Cost \
  --start-date 2026-06-01 --end-date 2026-12-31
# then add 50/80/100% notification thresholds in the portal (Cost Management → Budgets)
```

For in-app visibility, `CostTracker` queries the Cost Management API for *accrued
spend* and reports estimated remaining credit (grant − spend). The consumer identity
needs the **Cost Management Reader** role on the subscription. Call it periodically
(e.g. daily) — cost data lags a few hours, so don't poll it per message:

```python
from azure.identity import DefaultAzureCredential
from mailflow.adapters.graph import CostTracker
from mailflow.adapters.graph.live import CredentialTokenProvider, HttpxTransport

tracker = CostTracker(
    subscription_id="<azure-subscription-id>",
    transport=HttpxTransport(),
    token_provider=CredentialTokenProvider(credential=DefaultAzureCredential()),  # ARM-scoped
    credit_grant=200.0,
)
print("spent:", tracker.accrued_cost(), "remaining ~", tracker.remaining_credit())
```

> Your dominant cost is the Event Hubs Standard namespace running 24/7 (~$11/mo per
> throughput unit) + a tiny per-event charge + cents of checkpoint storage — so a few
> mailboxes barely dent $200; the 30-day expiry usually hits first.
