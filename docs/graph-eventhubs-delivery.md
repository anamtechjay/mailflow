# Graph → Azure Event Hubs delivery (no webhook) — analysis & implementation plan

> **Question:** can Microsoft Graph push Outlook change notifications **directly to
> Azure Event Hubs without a public webhook URL?**
> **Answer: YES — it's an officially supported, GA delivery channel.** Verified
> against learn.microsoft.com (`change-notifications-delivery-event-hubs`, updated
> Aug 2025). This doc analyses the three delivery options, recommends one, and
> gives a concrete implementation plan + flow diagram.

---

## 1. Verdict — is Option 1 possible?

**Yes.** Microsoft Graph supports three change-notification **delivery channels** for
the *same* subscription mechanism: **Webhooks**, **Azure Event Hubs**, and **Azure
Event Grid**. Only the `notificationUrl` changes; everything else about the
subscription (resource, `Mail.Read` app permission, 7-day lifetime, renewal,
`changeType`) stays the same.

What Event Hubs delivery gives you, verbatim from the docs:
- *"You don't rely on publicly exposed notification URLs. The Event Hubs SDK relays
  the notifications to your application."* → **no public HTTPS endpoint, no ingress,
  no TLS cert for a webhook, no firewall hole.**
- *"You don't need to reply to the notification URL validation. You can ignore the
  validation message that you receive."* → **no 10-second validation-handshake code.**
- Built for *"high throughput scenarios … applications subscribing to a large set of
  resources … multitenant applications."* → **scales far past a webhook endpoint.**

This is exactly your **Option 1** in the diagram, and it's the strongest fit if you
want to avoid exposing a public URL.

---

## 2. How it actually works (the mechanism)

A Graph subscription with an Event Hubs `notificationUrl` makes Graph **send**
notifications into your event hub; your app **reads** them from the hub with the
Event Hubs SDK (consumer group + checkpoint). No inbound traffic to you.

### `notificationUrl` formats (verified)
**RBAC (recommended — SAS is being deprecated):**
```
EventHub:https://<namespace>.servicebus.windows.net/eventhubname/<eventhubname>?tenantId=<domain>
```
**Key Vault + SAS (legacy, avoid):**
```
EventHub:https://<keyvault>.vault.azure.net/secrets/<secret>?tenantId=<domain>
```
- `<domain>` = the tenant's **primary domain** (e.g. `contoso.com`). ⚠️ It **must
  match the domain of the Azure subscription that holds the event hub** — see §6.

### Auth: who's allowed to write to the hub
Graph writes as the **"Microsoft Graph Change Tracking"** service principal
(**appId `0bf30f3b-4a52-48df-9a82-234910c4a086`**). You grant it the
**`Azure Event Hubs Data Sender`** role on the hub (RBAC). That's the *only* grant
needed for the RBAC path — **no Key Vault, no connection strings.**
- If that service principal is missing from the tenant, create it (the docs give the
  `POST /servicePrincipals {appId:"0bf30f3b-…"}` call).

### Validation message (ignore it)
Right after subscription creation, a **validation event lands on the hub** with
`subscriptionId:"NA"` and `changeType:"Validation: Testing client application
reachability…"`. Your consumer just **skips any event whose `subscriptionId == "NA"`**.
There is no token to echo.

### Payload on the hub
Same `changeNotificationCollection` JSON as a webhook would receive — for **basic**
(id-only) subscriptions: `subscriptionId`, `changeType`, `resource`, `clientState`,
`tenantId`, `resourceData.id`. `clientState` is still delivered (validate it).

### Big rich payloads (only relevant if you choose rich/encrypted)
Event Hubs max message size is **1 MB**. Rich notifications above 1 MB require a blob
storage account (container **must** be named `microsoft-graph-change-notifications`)
referenced via a `blobStoreUrl`; oversized events then carry an
`additionalPayloadStorageId` pointing to the blob. **Not needed for basic + re-fetch**
(our chosen model — payloads are tiny).

### What does NOT change vs webhook
- **Subscription lifetime & renewal:** still **7 days** (basic) / **1 day** (rich) for
  `message`; still renew on a timer (hourly is fine) and **recreate on 404**. Delivery
  channel doesn't change the subscription's lifetime.
- **`Mail.Read` (application)** permission + admin consent. Same.
- **Lifecycle notifications** (`reauthorizationRequired`/`subscriptionRemoved`/`missed`)
  still apply — and `lifecycleNotificationUrl` can ALSO be an `EventHub:` URL.
- **409** on duplicate subscription; **45-min** expiry floor.
- **Identity / dedup:** Event Hubs has **no broker dedup** (unlike Service Bus) — you
  keep your own idempotency key (we already do: `internetMessageId` / `(tenant,
  mailbox, provider_message_id)`).

---

## 3. The three options compared

| | **Webhook** (current/ref project) | **Option 1 — Event Hubs (direct)** ⭐ | **Option 2 — Event Grid → Service Bus** |
|---|---|---|---|
| Public HTTPS endpoint | **Required** | **Not required** | Not required |
| Validation handshake code | Required (echo token ≤10 s) | **Not needed** (ignore "NA" event) | Handled by Event Grid |
| Throughput | Limited by your endpoint; Graph drops if you're slow | **Very high** (stream, buffered) | High |
| Durable buffer if consumer is down | No (needs a queue after it) | **Yes — the hub IS the buffer** (retention) | Yes (Service Bus) |
| Delivery shape | Push to your URL | **You pull** (EH SDK, partitions, offsets) | Push → Service Bus → you pull |
| Per-message lock / complete / DLQ | n/a (you add a queue) | **No** — checkpoint by offset; build your own poison routing | **Yes** (Service Bus native) |
| Broker dedup | No | **No** | **Yes** (`message_id`) |
| Ordering | none | per-partition | per-session (SB) |
| Fan-out to N consumers | no | consumer groups | Event Grid multi-sub |
| Extra Azure infra | none (your app) | **EH namespace + hub + 1 RBAC role** | Event Grid partner topic + Service Bus |
| Maturity | GA | **GA** | GA |
| Best when | you already have a public API | **no public URL wanted; high volume; want a built-in buffer** | you want routing/filtering to many sinks + SB semantics |

**Recommendation:** **Option 1 (Event Hubs direct).** It deletes two boxes from the
reference design at once — **the webhook *and* the separate Service Bus queue** —
because the hub is itself the durable buffer. Lowest moving parts for "no public URL".
Choose Option 2 only if you specifically need Service Bus's per-message
lock/complete/dead-letter semantics or Event Grid's multi-sink routing.

---

## 4. Recommended flow (one idea)

```
                         subscription create (notificationUrl = EventHub:…)
                         + hourly renewal / recreate-on-404
                    ┌──────────────────────────────────────────────┐
                    │                                              ▼
        ┌───────────────────────┐   change notifications   ┌──────────────────────┐
        │   Microsoft Graph     │ ───────────────────────▶ │   Azure Event Hubs   │
        │  Outlook mailboxes    │   (Graph Change Tracking  │  "graph-notifications"│
        │  inbox + sentitems    │    = Data Sender on hub)  │  partitions · retention│
        └───────────▲───────────┘                          └──────────┬───────────┘
                    │                                                  │  EH SDK
        5 · re-fetch full message                                     │  (consumer group
            (body, uniqueBody, attachments)                           │   + blob checkpoint)
            by Graph id → fallback internetMessageId                  ▼
        ┌───────────┴───────────────────────────────────┐  ┌──────────────────────┐
        │              email_processing                  │◀─│   Notification        │
        │  parse envelope → filter → (classify) →        │  │   consumer            │
        │  extract CleanEmail → emit                     │  │  • skip "NA" validation│
        │  (mailflow pipeline / Dagster job / LangGraph) │  │  • verify clientState  │
        └────────────────────────────────────────────────┘  │  • extract ids only    │
                    ▲                                        │  • checkpoint offset   │
                    │                                        └──────────────────────┘
        Safety nets (unchanged from ref project):
          • 30s scheduled sweep of unread mail (missed events)
          • watermark / delta replay on outage (idempotent)
          • dedup on internetMessageId (no broker dedup in EH)
          • poison → your own DLQ (table / SB), since EH has no dead-letter
```

**The principle is identical to the webhook design** ("thin notification now, fetch
the body later") — you've just swapped the *transport* from `webhook + Service Bus`
to `Event Hubs`, and deleted the public endpoint.

---

## 5. Implementation plan

### Phase A — Azure provisioning (one-time, per Azure subscription)
1. Create an **Event Hubs namespace** (Standard tier; Basic works for SAS-only — use
   Standard for RBAC + consumer groups) and an **event hub** (e.g. `graph-notifications`,
   start with 2–4 partitions, retention 1–7 days = your outage buffer).
2. On the namespace/hub → **Access Control (IAM)** → assign **`Azure Event Hubs Data
   Sender`** to **"Microsoft Graph Change Tracking"** (appId `0bf30f3b-…`). *(Create
   that service principal first if it's missing — §2.)*
3. For the **consumer**, create a separate **`Azure Event Hubs Data Receiver`** grant
   (least privilege — don't reuse the sender identity) and a **blob container** for
   the EH checkpoint store.
4. Note the **primary domain** of the tenant — it must equal the Azure subscription's
   directory domain (§6).

### Phase B — Subscription management (replaces the webhook subscribe code)
5. Create the Graph subscription exactly as for webhooks, but:
   ```
   POST /subscriptions
   {
     "changeType": "created,updated",
     "resource": "users/{id}/mailFolders('inbox')/messages",   // + sentitems
     "notificationUrl": "EventHub:https://<ns>.servicebus.windows.net/eventhubname/graph-notifications?tenantId=<domain>",
     "lifecycleNotificationUrl": "EventHub:https://<ns>.servicebus.windows.net/eventhubname/graph-notifications?tenantId=<domain>",
     "clientState": "<secret>",
     "expirationDateTime": "<now + ~6 days>"
   }
   ```
   One subscription per **mailbox × folder** (Inbox + Sent), as decided.
6. **Renewal job** (hourly): `PATCH` each subscription's `expirationDateTime`;
   on **404**, recreate + delta-replay the gap. (Same as the reference project's
   `scheduling_service`.)

### Phase C — The notification consumer (replaces webhook callback + Service Bus sensor)
7. Use the **Event Hubs SDK** (`azure-eventhub` for Python) with a **consumer group**
   and a **blob checkpoint store** (`azure-eventhub-checkpointstoreblob-aio`). It
   pulls events, tracks offsets, and resumes after restart.
8. Per event:
   - **Skip** if `subscriptionId == "NA"` (validation message).
   - **Verify `clientState`** matches what you set (constant-time); drop + log if not.
   - **Extract ids only**: `userId` (from `resource`), `messageId` (`resourceData.id`),
     `changeType`. *No Graph call here* — same hot-path discipline.
   - Hand the thin pointer to the **pipeline** (mailflow) / emit a `RunRequest`
     (Dagster) keyed by `internetMessageId`/`messageId` for dedup.
   - **Checkpoint** the offset after the pointer is durably accepted.
9. **Fetch stage (unchanged):** the pipeline re-fetches the full message from Graph
   (`$select=… , uniqueBody, internetMessageHeaders`), with the `internetMessageId`
   fallback for folder moves, then runs parse → filter → extract → emit.

### Phase D — Reliability (port the reference project's safety nets)
10. **Dedup** on `internetMessageId` (EH has no broker dedup) — mailflow already has
    the idempotency key; keep it.
11. **Poison handling:** EH can't dead-letter a single message. On repeated failure,
    route the pointer to **your own DLQ** (a Service Bus queue or a DB table) and
    checkpoint past it, so one bad message can't wedge a partition.
12. **Missed-event sweep (30s)** + **delta/watermark replay** on outage — same as today.
13. **Subscription renewal** monitoring (alert if last successful renew is too old).

### What you can delete vs the webhook design
- ❌ The public **webhook endpoint** (ingress, TLS cert, validation handshake, IP
  allowlist, "slow/drop" endpoint-throttling worries).
- ❌ The **separate Service Bus queue** *(optional)* — the hub is the durable buffer.
  *(Keep a small Service Bus only if you want native DLQ for poison — Phase D.11.)*

---

## 6. Gotchas to flag before committing

1. **tenantId / Azure-subscription domain match (BLOCKER for multi-tenant):** the
   `tenantId=<domain>` in the `notificationUrl` must match the domain of the Azure
   subscription that owns the event hub. For a **single-tenant** deploy (you own both
   the mailboxes' tenant and the Azure sub) this is trivial. For **multi-tenant SaaS**
   (watching many customer tenants) you must design where the hub lives and how
   cross-tenant `Data Sender` is granted — verify before scaling.
2. **Stream, not a queue:** no per-message lock/complete/abandon/dead-letter. You
   checkpoint by offset and own poison routing. Different mental model from the
   reference project's Service Bus.
3. **No broker dedup** — keep app-level idempotency.
4. **SAS is deprecated** — use the **RBAC** path (`Data Sender` role), skip Key Vault.
5. **Ordering** is per-partition only; partition key = mailbox keeps a mailbox's
   events ordered if that matters.
6. **Renewal is still required** — Event Hubs does not extend subscription lifetime.
7. **Cost:** Event Hubs bills throughput units / processing units even at low volume;
   for a handful of mailboxes a webhook may be *cheaper*, while EH wins at scale.

---

## 7. Mapping to mailflow ports

| Concern | mailflow port / piece | Event Hubs binding |
|---|---|---|
| Receive notifications | (new) `EventHubsNotificationSource` (adapter-internal, not a core port) | EH SDK consumer group + blob checkpoint |
| Create/renew subscriptions | `SubscriptionManager` (`ensure_watch`/`renew_watch`) | `notificationUrl = EventHub:…`; lifecycle adapter-internal |
| Fetch full message | `MailboxProvider.fetch` (`GraphProvider`) | re-fetch by id / internetMessageId |
| Parse → filter → extract → emit | existing core pipeline | unchanged |
| Dedup / cursor | `DedupeStore` / `CursorStore` | internetMessageId + delta token |
| Secrets | `SecretProvider` | EH receiver credential / Graph client secret |

The webhook box in `graph-connection.md` is simply **replaced** by an Event Hubs
source; the rest of the Graph adapter design is unchanged. Recommend adding a
"delivery channel: webhook | eventhub" config switch so both are supported.

> **Implemented (2026-06-15):** the live wiring now exists —
> `mailflow.adapters.graph.live.run_consume_loop` (sync `EventHubConsumerClient` +
> `BlobCheckpointStore`), `EventHubCheckpointer` (offset commit per batch),
> `build_graph_runtime` composition root, `SubscriptionReconciler` (create/renew),
> `GraphLifecycleHandler` (reauthorizationRequired/subscriptionRemoved/missed), and
> `GraphProvider.sweep` (delta catch-up). See
> `docs/superpowers/plans/2026-06-15-mailflow-graph-eventhubs-live-flow.md`. Azure
> SDK imports stay local to `live.py`; the unit suite runs with no Azure installed.

---

## Sources
- Receive change notifications through Azure Event Hubs (notificationUrl formats, Data Sender role, ignore-validation, 1 MB/blobStoreUrl, appId 0bf30f3b-…) — https://learn.microsoft.com/en-us/graph/change-notifications-delivery-event-hubs
- Change notifications overview (channels, lifetimes, limits) — https://learn.microsoft.com/en-us/graph/change-notifications-overview
- Subscribe to Microsoft Graph events using Azure Event Grid (Option 2) — https://learn.microsoft.com/en-us/azure/event-grid/subscribe-to-graph-api-events
- Microsoft Graph API events in Azure Event Grid (partner topic) — https://learn.microsoft.com/en-us/azure/event-grid/partner-events-graph-api
- Authorize access to Azure Event Hubs (RBAC vs SAS) — https://learn.microsoft.com/en-us/azure/event-hubs/authorize-access-event-hubs
