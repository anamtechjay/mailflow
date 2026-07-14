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

### Filters, stages, and retrieval (the black box)

One `filters` list — built-in specs, your own functions, or filter objects (all mixed):

```python
mf = connect("gmail", credentials=creds, mailbox="me",
    filters=[
        {"kind": "blacklist", "domains": ["spam.com"]},   # built-in spec
        {"kind": "no_personal"},                          # block gmail/yahoo/…
        lambda env: "invoice" in env.subject.lower(),     # your own function (True=keep)
    ],
    stages=[enrich, route],          # post-process each CleanEmail (return None to drop)
    clean_fn=my_clean,               # optional: tweak the cleaned email
)
```
`filters` defaults to empty → nothing is dropped (safe-by-default). Custom filter functions
get the `Envelope` and return `True` (keep) / `False` (drop). Stages get the full
`CleanEmail` and may modify it or drop it (return falsy).

**What happens to filtered (matched) emails:** By default, a filter match **tags** the email
and delivers it anyway — no silent drops. Use `on_filtered` to control this:

```python
mf = connect("gmail", credentials=creds, mailbox="me",
    filters=[{"kind": "blacklist", "domains": ["spam.com"]}],
    on_filtered="tag",           # (default) deliver with disposition="filtered"
)
# Or to restore the old behavior (drop matched emails):
mf = connect("gmail", credentials=creds, mailbox="me",
    filters=[{"kind": "blacklist", "domains": ["spam.com"]}],
    on_filtered="drop",          # silently suppress matched emails
)
```

When `on_filtered="tag"` (the default), a filter-matched email arrives with:
- `disposition == "filtered"` (instead of `"emitted"`)
- `filter_reason` — the description of which filter(s) matched
- `matched_filter` — the matched filter spec or function

This lets you decide: archive filtered mail, skip processing, route to a secondary handler, etc.

```python
for email in mf.stream():
    if email.disposition == "filtered":
        archive_but_dont_process(email)   # you decide: keep / skip / route
    else:
        handle(email)
```

**Breaking change:** Prior versions dropped filter-matched emails silently. If your code relies
on that suppression, pass `on_filtered="drop"` to restore it. New code should leave the
default (`"tag"`) and explicitly handle the `disposition == "filtered"` case.

Select only the data you need — just name the fields (smaller payload, self-documenting):

```python
mf = connect("gmail", credentials=creds, mailbox="me",
             fields=["subject", "from", "attachments"])

for email in mf.stream():
    # email is a dict with ONLY those keys:
    #   {"subject": "...", "from": <Recipient>, "attachments": [...]}
    ...
```
`fields=None` (default) yields the full `CleanEmail`. `"from"` is an alias for `from_`.
Three knobs: `filters` (which emails) · `fields` (which data) · `stages` (process each).

Pull any part on demand by message ID (no need to store emails):
```python
email = mf.get_email(message_id)        # -> CleanEmail
mf.get_body(message_id)                 # -> str
mf.get_recipients(message_id)           # -> list[Recipient]
mf.get_attachments(message_id)          # -> list[Attachment]
```

**State (`state=`):**
- `"memory"` (default) — stateless; on restart you may re-receive recent mail, so **dedupe
  on `email.canonical_id`** (always present, stable).
- `"sqlite:///path/mf.db"` — persists cursor + dedupe to one file; a restart resumes where
  it stopped. Pure stdlib `sqlite3`, no extra dependency.

## Logs & observability

mailflow ships **silent** (a `NullHandler`). Turn logs on where you want them:

```python
from mailflow import connect

# 1) Send diagnostic logs to a rotating file (or omit log_file for stderr):
mf = connect("gmail", credentials=creds, mailbox="me",
             log_file="mailflow.log", log_level="INFO")

# 2) Pass your own function(s) that receive every per-email decision as structured data:
def audit(trace):            # DecisionTrace: canonical_id, disposition, matched_filter, reason
    db.insert(trace.model_dump())

def metrics(report):         # RunReport: emitted / dropped / duplicates / dead_lettered
    statsd.gauge("mailflow.emitted", report.emitted)

mf = connect("gmail", credentials=creds, mailbox="me",
             on_trace=audit, on_report=metrics)
```

`on_trace` fires once per message (including **dropped/filtered/duplicate/dead-lettered** — so a
"missing" email is always explained by its `matched_filter` + `reason`). `on_report` fires once per
run with the counters. A callback that raises is logged and swallowed — it never interrupts
ingestion. Both default to off; combine them freely with `log_file`. For full control, ignore these
and attach your own handler to `logging.getLogger("mailflow")`.

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

The wire contract is `EmailEvent` / `CleanEmail` at `SCHEMA_VERSION = "1.1"` (the `EmailEvent`
now carries an `idempotency_key`). Adapters plug into the ports in `mailflow.core.ports`
without touching `core`.

**Delivery & concurrency (V1):** delivery is **at-least-once** — duplicates can occur, so every
`EmailEvent` carries an `idempotency_key` (`tenant|mailbox|provider_message_id`) for
consumer-side dedupe. V1 is **synchronous, single-worker** (`mailflow.core.ports.SYNC_ONLY =
True`); a future async family is an additive change, not a breaking one.

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

## Live: Outlook via Event Grid → Azure Service Bus (no Event Hubs)

Alternative to Event Hubs: ingest Outlook via **Microsoft Graph change notifications delivered
to Azure Service Bus queues** (via Event Grid Partner Topic). This removes the Event Hubs dependency;
use it when Event Hubs slots are constrained or you prefer direct Service Bus integration.

The ingress chain: **Outlook → Microsoft Graph → Event Grid Partner Topic → Service Bus queue → mailflow**.
Install the Service Bus extra:

```bash
pip install -e ".[servicebus,graph]"
```

Provision Service Bus + Event Grid Partner Topic per `docs/azure-servicebus-setup.md`, then
configure the graph subscription and run:

Ingress — `run_service` builds the Graph token provider + a Service Bus receiver internally
and blocks, feeding each Event Grid CloudEvent through the Graph pipeline:

```python
from mailflow.adapters.graph import GraphConfig
from mailflow.adapters.servicebus.config import ServiceBusConfig
from mailflow.adapters.servicebus.live import run_service
from mailflow.secrets import EnvSecretProvider
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

graph_cfg = GraphConfig.model_validate({
    "tenant_id": "<tenant-guid>", "client_id": "<app-guid>",
    "client_secret_ref": "env://GRAPH_CLIENT_SECRET",
    "mailboxes": ["ops@acme.com"],
})
servicebus_cfg = ServiceBusConfig(
    fully_qualified_namespace="<SB_NAMESPACE>.servicebus.windows.net",
    entity_name="mailflow-graph",
    connection_string_ref="env://SB_CONNECTION_STRING",
)

run_service(                       # blocks, consuming the Service Bus queue
    graph_cfg=graph_cfg, servicebus_cfg=servicebus_cfg, tenant="acme",
    secret_provider=EnvSecretProvider(),
    emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
    cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
    blob_store=InMemoryBlobStore(),
    connection_string="<sb-connection-string>",   # OR credential=DefaultAzureCredential()
)
```

Egress — separately, `ServiceBusEmitter` is a drop-in `Emitter` you can plug into ANY provider
(gmail/graph/memory) via `connect(..., overrides={"emitter": ...})`, to publish outbound
`EmailEvent`s to a Service Bus queue instead of the default in-memory sink:

```python
from mailflow import connect
from mailflow.adapters.servicebus import ServiceBusEmitter
from mailflow.adapters.servicebus.live import AzureServiceBusSender
from azure.servicebus import ServiceBusClient

sb_client = ServiceBusClient.from_connection_string("<servicebus-conn>")  # OR credential=...
sender = sb_client.get_queue_sender(queue_name="mailflow-egress")

mf = connect(
    "gmail", credentials=creds, mailbox="me",
    overrides={"emitter": ServiceBusEmitter(sender=AzureServiceBusSender(sender=sender))},
)
mf.run()
```

**Contract Decision 1 (CD-1):** DLQ ownership follows the pipeline model—`Pipeline` performs
per-message dead-lettering before handing off to the emitter; the runtime completes (acks)
after `run_once()` finishes. One poison message ⇒ exactly one mailflow DLQ record.

**Contract Decision 2 (CD-2):** Microsoft Graph's Event Grid integration may report `created`
events for messages irregularly. If `"created"` subscription is rejected, fall back to
`"updated"` alone and rely on `GraphProvider.sweep` timer (daily delta-replay) for new-mail
catch-up. See `docs/azure-servicebus-setup.md` step 3.

### Choosing the Graph transport: `delivery=`

Same Graph mail, your choice of transport — only one argument changes:

```python
# Event Hubs (default) — needs the [graph] extra
mf = connect("graph", delivery="eventhub", credentials={
    "tenant_id": "<tenant-guid>", "client_id": "<app-guid>",
    "client_secret_ref": "env://GRAPH_CLIENT_SECRET", "mailboxes": ["ops@acme.com"],
    "namespace": "evh-mailflow", "hub": "graph-notifications", "tenant_domain": "acme.com",
    "connection_string": "<eventhub-connection-string>",   # or omit + pass credential=
})

# Service Bus (Event Grid Partner Topic → Service Bus) — needs the [servicebus,graph] extras
mf = connect("graph", delivery="servicebus", credentials={
    "tenant_id": "<tenant-guid>", "client_id": "<app-guid>",
    "client_secret_ref": "env://GRAPH_CLIENT_SECRET", "mailboxes": ["ops@acme.com"],
    "fully_qualified_namespace": "<ns>.servicebus.windows.net", "entity_name": "mailflow-graph",
    "connection_string": "<servicebus-connection-string>",  # or omit + pass credential=
})

for email in mf.stream():   # identical downstream, whichever transport you chose
    my_app.save(email)
```

`delivery` defaults to `"eventhub"`, so existing `connect("graph", …)` code is unchanged. It
applies only to the `"graph"` provider. Install the matching extra:
`pip install -e ".[graph]"` for Event Hubs, `pip install -e ".[servicebus,graph]"` for Service
Bus. Provisioning for the Service Bus path is in `docs/azure-servicebus-setup.md`.

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

### V1 notes

- **Refresh-token rotation (A8):** if Google rotates the OAuth refresh token, pass a
  `TokenRotationSink` so the new token is persisted for the next run — e.g.
  `run_service(..., rotation_sink=FileTokenRotationSink("tokens.json"))` (from
  `mailflow.adapters.gmail.rotation`), or via `connect(overrides={"rotation_sink": ...})`.
  Without a sink, rotation is detected but not saved and the next run may fail to auth.
- **Push verification (A5):** `GmailWebhookVerifier` (`mailflow.adapters.gmail.webhook`)
  validates an OIDC-JWT and returns identity only. It applies to **HTTP push** delivery
  (verify the `Authorization` bearer before processing). The default runtime uses Pub/Sub
  **pull**, which is authenticated by the subscriber's own GCP credentials and needs no
  webhook verification — so the verifier is a seam to wire in only if you add an HTTP push
  endpoint. V1 is poll-authoritative; push only wakes the poller.

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
