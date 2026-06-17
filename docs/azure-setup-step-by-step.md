# Azure setup — step-by-step (portal click-by-click)

A beginner-friendly runbook to connect **mailflow** to Azure so Outlook mail events
flow into an Event Hub. Follow top to bottom when setting up a **new account**.
(For the `az` CLI version, see `superpowers/plans/2026-06-11-microsoft-graph-eventhubs-provisioning.md`.)

> **Placeholders** look like `<this>` — replace with your own values. Never commit
> real secrets; put them in a git-ignored `.env` (template: `../.env.example`).

---

## 0. Before you start — the ONE thing that blocks people

Microsoft Graph app-only mail (`Mail.Read`) **only works on an Exchange Online
(work/school) mailbox** — i.e. `name@<tenant>.onmicrosoft.com`. It does **NOT** work
on personal **Gmail** or **Outlook.com** accounts.

If you signed up for Azure with a **Gmail**, your tenant has **no mailbox yet**, and
your Gmail (a *personal* account) **cannot log into the Microsoft 365 admin center**.
You'll fix that in **Step 5** (create a work-admin account + start a free mailbox trial).

What you'll end up with (all values land in `.env`):

```
tenant_id, tenant_domain, client_id, client_secret, mailboxes,
namespace, hub, consumer_group, connection_string,
checkpoint_connection_string, checkpoint_container, subscription_id
```

---

## Step 1 — Tenant ID + Domain  (→ `GRAPH_TENANT_ID`, `GRAPH_TENANT_DOMAIN`)
1. Portal left menu (or search bar) → **Microsoft Entra ID**.
2. On **Overview**, copy:
   - **Tenant ID** (a GUID) → `GRAPH_TENANT_ID`
   - **Primary domain** (e.g. `<tenant>.onmicrosoft.com`) → `GRAPH_TENANT_DOMAIN`

## Step 2 — App registration → Client ID  (→ `GRAPH_CLIENT_ID`)
1. In **Microsoft Entra ID** → **App registrations** → **+ New registration**.
2. Name: `mailflow-graph`. Account types: **Single tenant**. → **Register**.
3. On the app **Overview**, copy **Application (client) ID** → `GRAPH_CLIENT_ID`.

## Step 3 — Client secret  (→ `GRAPH_CLIENT_SECRET`)
1. App → **Certificates & secrets** → **+ New client secret**.
2. Description `mailflow`, expiry 6–12 months → **Add**.
3. **Copy the secret `Value` immediately** (shown only once; NOT the "Secret ID")
   → `GRAPH_CLIENT_SECRET`. If you miss it, just create a new one.

## Step 4 — Mail.Read permission + admin consent
1. App → **API permissions** → **+ Add a permission** → **Microsoft Graph** →
   **Application permissions**.
2. Search **Mail.Read** → tick → **Add permissions**.
3. **Grant admin consent for <directory>** → confirm. Status must show a **green ✓**.
   *(If "Grant admin consent" is greyed out, you're not a Global Admin — see Step 5.)*

## Step 5 — Get a mailbox to watch  (→ `GRAPH_MAILBOXES`)

> Skip if you already have an Exchange Online mailbox in this tenant — just use its
> address. Otherwise (Gmail-based tenant), do this:

**5a. Create a work-admin account** (because a personal Gmail can't access M365 admin):
1. **Microsoft Entra ID → Users → + New user → Create new user**.
2. User principal name: `admin` → pick the `…onmicrosoft.com` domain → `admin@<tenant>.onmicrosoft.com`.
3. Set a password (save it) → **Create**.
4. **Microsoft Entra ID → Roles and administrators → Global Administrator →
   + Add assignments** → add `admin@…`.

**5b. Start a free mailbox trial:**
1. **Incognito** window → **admin.microsoft.com** → sign in as `admin@<tenant>.onmicrosoft.com`
   (set MFA + change password when prompted).
2. **Billing → Purchase services → Microsoft 365 Business Basic → Start free trial**
   (free 30 days; a card may be requested but isn't charged — cancel before day 30 to avoid a bill).

**5c. Create the mailbox user:**
1. **Users → Active users → Add a user** → username `ops` → assign the **Business Basic** license.
2. → `GRAPH_MAILBOXES=ops@<tenant>.onmicrosoft.com`

> **If trials are blocked** in your tenant: use the **Microsoft 365 Developer Program**
> (free E5 mailboxes) — but it's a *separate* tenant, so you'd recreate Steps 6–9 there.

## Step 6 — Event Hubs namespace + hub + consumer group  (→ `namespace`, `hub`, `consumer_group`)
1. Search → **Event Hubs** → **+ Create**.
2. **Resource group:** Create new → `mailflow` (any name; just reuse it for everything).
3. **Namespace name:** globally unique, lowercase (e.g. `mailflow` or `evh-mailflow-<you>`).
   **Pricing tier: Standard** (required for consumer groups + RBAC). **Throughput units: 1**.
   **Region:** nearest (India → **Central/South India**). → **Review + create** → **Create**.
   - → `EVENTHUB_NAMESPACE` = your namespace name.
4. Open the namespace → **+ Event Hub** → name `graph-notifications` → **Create**.
   - → `EVENTHUB_HUB = graph-notifications`.
5. Open `graph-notifications` → **Consumer groups** → **+ Consumer group** → `mailflow`.
   - → `EVENTHUB_CONSUMER_GROUP = mailflow`.

## Step 7 — Event Hub connection string  (→ `EVENTHUB_CONNECTION_STRING`)
1. Namespace → **Settings → Shared access policies** → **RootManageSharedAccessKey**.
2. Copy **Connection string–primary key** (starts `Endpoint=sb://…;SharedAccessKey=…`)
   → `EVENTHUB_CONNECTION_STRING`. **Secret — keep it out of git.**

## Step 8 — Let Graph write to your hub (Data Sender role) — the key link
1. Namespace → **Access control (IAM)** → **+ Add → Add role assignment**.
2. **Role:** search `Data Sender` → **Azure Event Hubs Data Sender** → **Next**.
3. **Members:** User, group, or service principal → **+ Select members** → search
   **Microsoft Graph Change Tracking** (appId `0bf30f3b-4a52-48df-9a82-234910c4a086`)
   → select → **Review + assign**.
   - If it doesn't appear: open **Cloud Shell** (`>_` icon, Bash) →
     `az ad sp create --id 0bf30f3b-4a52-48df-9a82-234910c4a086` → retry.

## Step 9 — Storage account + container  (→ `CHECKPOINT_CONNECTION_STRING`, `CHECKPOINT_CONTAINER`)
1. Search **Storage accounts** → **+ Create** → same RG `mailflow`.
   - **Name:** lowercase/unique (e.g. `stmailflow<you>`). **Preferred storage type:**
     **Azure Blob Storage**. **Performance:** Standard. **Redundancy:** **LRS** (cheapest).
   - → **Review + create** → **Create** → **Go to resource**.
2. **Data storage → Containers → + Container** → name `eh-checkpoints`.
   - → `CHECKPOINT_CONTAINER = eh-checkpoints`.
3. **Security + networking → Access keys → Show** → copy **Connection string** (key1)
   → `CHECKPOINT_CONNECTION_STRING`. **Secret.**

## Step 10 — Subscription ID  (→ `AZURE_SUBSCRIPTION_ID`)
1. Search **Subscriptions** → click yours → copy **Subscription ID** → `AZURE_SUBSCRIPTION_ID`
   (used by `CostTracker` for credit tracking).

---

## After collecting everything — fill `.env`
Copy `.env.example` to `.env` and paste each value. Then run:

```powershell
pip install -e ".[graph]"
# load .env into this terminal:
Get-Content .env | Where-Object {$_ -match '=' -and -not $_.StartsWith('#')} | ForEach-Object { $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim()) }
```

- **Prove the hub + pipeline live without a mailbox:** run `scripts/demo_live_consume.py`
  in one terminal, `scripts/send_test_event.py` in another → a `CleanEmail` prints.
- **Real end-to-end (needs Step 5 mailbox):** wire `run_service(...)` (see `../README.md`),
  start it, then email the `ops@…` mailbox → it flows through.

---

## Cost / credit notes
- Azure free trial = **$200, expires 30 days** after signup regardless of usage.
- Dominant cost is the **Event Hubs Standard namespace ~$11/mo per throughput unit** +
  cents of storage. A few mailboxes barely dent $200.
- The **M365 mailbox trial is separate billing** (not the Azure $200): free 30 days, then paid.
- **Delete the `mailflow` resource group** when done to stop all Azure charges.
- Track spend in-app with `CostTracker`, or set a native **Budget** alert (see README).

---

## Troubleshooting (gotchas we hit)
| Symptom | Cause / fix |
|---|---|
| "You can't sign in here with a personal account" at admin.microsoft.com | Gmail is personal → create the work-admin account (Step 5a) and sign in with it. |
| Namespace name "not available" | It's global — add digits (`mailflow-<you>25`). |
| No "Consumer groups" / RBAC option | Namespace is **Basic** — must be **Standard** (recreate). |
| "Microsoft Graph Change Tracking" not found in Step 8 | SP missing — `az ad sp create --id 0bf30f3b-4a52-48df-9a82-234910c4a086`. |
| Subscription create returns 400 …notificationUrl validation | Step 8 Data Sender role not applied, or `tenant_domain` wrong. |
| `GET /users/<mbx>/messages` returns 403 | Mailbox not licensed, or `Mail.Read` consent not granted (Step 4). |
| Graph can't read mail at all | Using Gmail/Outlook.com — must be an Exchange Online mailbox (Step 0). |
