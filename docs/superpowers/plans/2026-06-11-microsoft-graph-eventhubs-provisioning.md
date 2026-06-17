# Microsoft (Graph + Event Hubs) Provisioning Plan

> **For operators:** this is an **admin runbook**, not a code plan. Steps are executed by a person with Azure/Entra/Exchange admin rights (some steps need different admins — see the RACI). Each step has an **exact command**, a **verification**, and **what to record** for mailflow config. Use checkboxes (`- [ ]`) to track. Tenant-specific values are placeholders in `<ANGLE_BRACKETS>` — fill them in Phase 0, never hardcode secrets.

**Goal:** Stand up everything on the Microsoft side so mailflow can receive Outlook change notifications via **Azure Event Hubs (no webhook)** and re-fetch messages with **app-only** auth — starting with **one test mailbox**, then scaling to **org-level multi-user**.

**Approach:** Entra app registration (app-only) → `Mail.Read` application permission + admin consent → Azure Event Hubs + grant the Graph **Change Tracking** service principal `Data Sender` → consumer access + blob checkpoint → store secrets → scope mailbox access with **RBAC for Applications** → smoke-test a real subscription end-to-end.

**Tooling:** Azure CLI (`az`) for Entra + Azure resources; **Exchange Online PowerShell** for RBAC-for-Applications scoping; `curl`/REST for the Graph smoke test. All values verified against learn.microsoft.com (see [Sources](#sources)).

**Decisions locked from research:**
- **App-only** (client credentials), permission **`Mail.Read` (Application)** — NOT `Mail.Read.Shared` (the `.Shared` scopes can't subscribe to change notifications).
- **Delivery = Event Hubs**, RBAC auth (SAS is deprecated) → no Key Vault needed for the hub connection.
- Graph writes to the hub as the **Microsoft Graph Change Tracking** SP, appId **`0bf30f3b-4a52-48df-9a82-234910c4a086`**, role **Azure Event Hubs Data Sender**.
- **Scope to specific mailboxes** with RBAC for Applications; **remove the broad Entra grant** or scoping is void (union rule).
- Subscriptions are **per mailbox × folder** (Inbox + Sent); no tenant-wide wildcard.

---

## RACI — who runs which phase

| Phase | Needs role |
|---|---|
| 1–2 App reg + permission + consent | **Entra Global Admin / App Admin** + **Global Admin** for consent |
| 3–5 Event Hubs + roles + storage | **Azure subscription Owner/Contributor + User Access Admin** |
| 6 RBAC for Applications scoping | **Exchange Online admin** |
| 7–8 Smoke test + subscriptions | **Eng** (using the app's own credentials) |

---

## Phase 0: Collect values & set shell variables

- [ ] **Step 1: Sign in and pick the subscription**

```bash
az login
az account set --subscription "<AZURE_SUBSCRIPTION_ID>"
az account show --query "{tenantId:tenantId, sub:id, domain:user.name}" -o table
```
Verify: the printed `tenantId` is the org tenant whose mailboxes you'll watch.

- [ ] **Step 2: Define variables used by later steps** (edit the values, then paste)

```bash
# --- identifiers you choose ---
APP_NAME="mailflow-graph"
RG="rg-mailflow-graph"
LOCATION="uksouth"
EH_NAMESPACE="evh-mailflow-$RANDOM"          # must be globally unique
EH_HUB="graph-notifications"
EH_CONSUMER_GROUP="mailflow"
STORAGE_NAME="stmailflowchk$RANDOM"          # 3-24 lc-alnum, globally unique
CHECKPOINT_CONTAINER="eh-checkpoints"
# --- fixed Microsoft values (do not change) ---
GRAPH_APP_ID="00000003-0000-0000-c000-000000000000"          # Microsoft Graph resource
MAIL_READ_ROLE_ID="810c84a8-4a9e-49e6-bf7d-12d183f40d01"      # Mail.Read (Application)
CHANGE_TRACKING_APP_ID="0bf30f3b-4a52-48df-9a82-234910c4a086" # Graph Change Tracking SP
# --- tenant facts (fill from Step 1) ---
TENANT_ID="<TENANT_ID>"
TENANT_DOMAIN="<acme.com>"                    # Entra primary domain (Overview page)
TEST_MAILBOX="<ops@acme.com>"                 # the one mailbox you test with first
```
Verify: `echo $EH_NAMESPACE $STORAGE_NAME` prints unique-looking names.

> ⚠️ **Confirm before running:** `MAIL_READ_ROLE_ID` is the well-known id for Mail.Read (Application). If a later consent step errors on it, re-derive with:
> `az ad sp show --id $GRAPH_APP_ID --query "appRoles[?value=='Mail.Read' && contains(allowedMemberTypes,'Application')].id" -o tsv`

- [ ] **Step 3: Create the resource group**

```bash
az group create --name "$RG" --location "$LOCATION" -o table
```
Verify: `az group show -n "$RG" --query properties.provisioningState -o tsv` → `Succeeded`.

---

## Phase 1: Entra app registration (app-only identity)

- [ ] **Step 1: Create the app registration + service principal**

```bash
APP_ID=$(az ad app create --display-name "$APP_NAME" --sign-in-audience AzureADMyOrg --query appId -o tsv)
az ad sp create --id "$APP_ID" -o none
echo "CLIENT_ID (appId) = $APP_ID"
```
Verify: `az ad app show --id "$APP_ID" --query displayName -o tsv` → `mailflow-graph`.
**Record:** `$APP_ID` → mailflow config `client_id`.

- [ ] **Step 2: Create a client secret** (quick start; prefer a certificate for prod — Step 3)

```bash
az ad app credential reset --id "$APP_ID" --display-name "mailflow-secret" \
  --years 1 --query "{clientId:appId, secret:password, tenant:tenant}" -o json
```
Verify: the JSON contains a `secret` value.
**Record:** put the `secret` into your **secret store** (Phase 5), referenced by mailflow `client_secret_ref`. **Do not** paste it into config files or git.

- [ ] **Step 3 (prod option): use a certificate instead of a secret**

```bash
# generate a self-signed cert + upload its public key to the app
az ad app credential reset --id "$APP_ID" --create-cert \
  --cert "mailflow-graph-cert" --query "{certThumbprint:certificateThumbprint}" -o json
```
Verify: `az ad app show --id "$APP_ID" --query "keyCredentials[].displayName" -o tsv` lists the cert.
**Record:** store the private key (`.pem` returned in `~/.azure`) in the secret store; mailflow uses cert auth instead of a secret. *(Skip if you used Step 2.)*

---

## Phase 2: API permission + admin consent

- [ ] **Step 1: Add the `Mail.Read` application permission**

```bash
az ad app permission add --id "$APP_ID" \
  --api "$GRAPH_APP_ID" \
  --api-permissions "${MAIL_READ_ROLE_ID}=Role"
```
Verify: `az ad app permission list --id "$APP_ID" --query "[].resourceAccess[].id" -o tsv` includes `$MAIL_READ_ROLE_ID`.

- [ ] **Step 2: Grant admin consent** (requires Global Admin)

```bash
az ad app permission admin-consent --id "$APP_ID"
```
Verify (wait ~1 min, then):
```bash
SP_OBJECT_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv)
az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_OBJECT_ID/appRoleAssignments" \
  --query "value[].appRoleId" -o tsv
```
Expected: includes `$MAIL_READ_ROLE_ID`. If empty, consent didn't apply — re-run as Global Admin in the portal (Entra → App registrations → API permissions → **Grant admin consent**).

> **Note:** at this point the app can read **every** mailbox in the tenant. Phase 6 narrows that. For the single-mailbox test you may proceed and scope later, but do not go to production unscoped.

---

## Phase 3: Azure Event Hubs + grant Graph "Data Sender"

- [ ] **Step 1: Create the namespace (Standard — needed for RBAC + consumer groups) and hub**

```bash
az eventhubs namespace create --name "$EH_NAMESPACE" --resource-group "$RG" \
  --location "$LOCATION" --sku Standard -o table
az eventhubs eventhub create --name "$EH_HUB" --namespace-name "$EH_NAMESPACE" \
  --resource-group "$RG" --partition-count 4 --cleanup-policy Delete --retention-time-in-hours 168 -o table
```
Verify: `az eventhubs eventhub show -n "$EH_HUB" --namespace-name "$EH_NAMESPACE" -g "$RG" --query name -o tsv` → `graph-notifications`.
**Record:** `$EH_NAMESPACE`, `$EH_HUB` → mailflow `EventHubConfig.namespace` / `.hub`. (168h retention = your 7-day outage buffer.)

- [ ] **Step 2: Create the consumer group mailflow reads from**

```bash
az eventhubs eventhub consumer-group create --name "$EH_CONSUMER_GROUP" \
  --eventhub-name "$EH_HUB" --namespace-name "$EH_NAMESPACE" --resource-group "$RG" -o table
```
Verify: `az eventhubs eventhub consumer-group list --eventhub-name "$EH_HUB" --namespace-name "$EH_NAMESPACE" -g "$RG" --query "[].name" -o tsv` includes `mailflow`.
**Record:** `$EH_CONSUMER_GROUP` → `EventHubConfig.consumer_group`.

- [ ] **Step 3: Ensure the Graph Change Tracking SP exists in the tenant**

```bash
CT_SP_ID=$(az ad sp show --id "$CHANGE_TRACKING_APP_ID" --query id -o tsv 2>/dev/null)
if [ -z "$CT_SP_ID" ]; then
  CT_SP_ID=$(az ad sp create --id "$CHANGE_TRACKING_APP_ID" --query id -o tsv)
fi
echo "Change Tracking SP objectId = $CT_SP_ID"
```
Verify: `$CT_SP_ID` is a non-empty GUID.

- [ ] **Step 4: Grant Change Tracking the "Azure Event Hubs Data Sender" role on the namespace**

```bash
EH_SCOPE=$(az eventhubs namespace show -n "$EH_NAMESPACE" -g "$RG" --query id -o tsv)
az role assignment create --assignee-object-id "$CT_SP_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Azure Event Hubs Data Sender" --scope "$EH_SCOPE" -o table
```
Verify: `az role assignment list --assignee "$CT_SP_ID" --scope "$EH_SCOPE" --query "[].roleDefinitionName" -o tsv` → `Azure Event Hubs Data Sender`.
**This is the grant that lets Graph publish notifications into your hub.**

- [ ] **Step 5: Compose the notificationUrl (RBAC form) you'll use when subscribing**

```bash
NOTIFICATION_URL="EventHub:https://${EH_NAMESPACE}.servicebus.windows.net/eventhubname/${EH_HUB}?tenantId=${TENANT_DOMAIN}"
echo "$NOTIFICATION_URL"
```
Verify: it matches `EventHub:https://<ns>.servicebus.windows.net/eventhubname/<hub>?tenantId=<domain>`.
> ⚠️ `tenantId` **must be the domain of the Azure subscription that holds this hub** (single-tenant: same org — fine).

---

## Phase 4: Consumer access + checkpoint store

- [ ] **Step 1: Create the storage account + checkpoint container**

```bash
az storage account create --name "$STORAGE_NAME" --resource-group "$RG" \
  --location "$LOCATION" --sku Standard_LRS -o table
az storage container create --name "$CHECKPOINT_CONTAINER" \
  --account-name "$STORAGE_NAME" --auth-mode login -o table
```
Verify: `az storage account show -n "$STORAGE_NAME" -g "$RG" --query provisioningState -o tsv` → `Succeeded`.
**Record:** `$STORAGE_NAME`, `$CHECKPOINT_CONTAINER` → mailflow checkpoint config.

- [ ] **Step 2: Grant the *mailflow consumer* identity "Data Receiver" on the hub** (least privilege — separate from the sender)

```bash
# If mailflow runs as a managed identity, use its objectId here; for local dev use your own SP/app.
CONSUMER_OBJECT_ID="<MAILFLOW_CONSUMER_OBJECT_ID>"   # e.g. managed identity of the container app
az role assignment create --assignee-object-id "$CONSUMER_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Azure Event Hubs Data Receiver" --scope "$EH_SCOPE" -o table
# checkpoint store needs blob read/write
az role assignment create --assignee-object-id "$CONSUMER_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "$(az storage account show -n "$STORAGE_NAME" -g "$RG" --query id -o tsv)" -o table
```
Verify: `az role assignment list --assignee "$CONSUMER_OBJECT_ID" --query "[].roleDefinitionName" -o tsv` lists both roles.

---

## Phase 5: Secret storage (no secrets in code)

- [ ] **Step 1: Store the app credential where the `SecretProvider` port resolves it**

```bash
# Example with Azure Key Vault (any secret store works; mailflow only sees a *reference*):
KV_NAME="kv-mailflow-$RANDOM"
az keyvault create --name "$KV_NAME" --resource-group "$RG" --location "$LOCATION" -o table
az keyvault secret set --vault-name "$KV_NAME" --name "graph-client-secret" \
  --value "<THE_SECRET_FROM_PHASE_1_STEP_2>" -o none
```
Verify: `az keyvault secret show --vault-name "$KV_NAME" --name graph-client-secret --query name -o tsv` → `graph-client-secret`.
**Record:** the reference (e.g. `kv://$KV_NAME/graph-client-secret`) → mailflow `GraphConfig.client_secret_ref`. *(Grant the mailflow runtime identity `Key Vault Secrets User` on this vault.)*

---

## Phase 6: Scope mailbox access with RBAC for Applications (Exchange Online)

> Run in **Exchange Online PowerShell** (`Connect-ExchangeOnline`) as an Exchange admin. This restricts the app from "all mailboxes" to only the ones you intend. **Start with the single test mailbox**, then switch the scope to a group for org-level.

- [ ] **Step 1: Connect**

```powershell
Install-Module ExchangeOnlineManagement -Scope CurrentUser   # once
Connect-ExchangeOnline -Organization "<acme.onmicrosoft.com>"
```
Verify: `Get-ConnectionInformation` shows a connected state.

- [ ] **Step 2 (single-mailbox test): scope the app to ONE mailbox**

```powershell
# Register the app for Exchange RBAC, scoped to one mailbox via a custom resource scope.
New-ManagementScope -Name "mailflow-test-mailbox" `
  -RecipientRestrictionFilter "PrimarySmtpAddress -eq '<ops@acme.com>'"

New-ServicePrincipal -AppId "<APP_ID>" -ObjectId "<SP_OBJECT_ID>" -DisplayName "mailflow-graph"

New-ManagementRoleAssignment -Name "mailflow-mail-read" `
  -App "<APP_ID>" -Role "Application Mail.Read" `
  -CustomResourceScope "mailflow-test-mailbox"
```
Verify: `Get-ManagementRoleAssignment -RoleAssignee "<APP_ID>" | Format-List Name,Role,CustomResourceScope` shows the scoped assignment.

- [ ] **Step 3 (org-level): switch the scope to a mail-enabled group**

```powershell
# Create/choose a group whose members are the mailboxes to watch, then scope to it.
New-ManagementScope -Name "mailflow-watched" `
  -RecipientRestrictionFilter "MemberOfGroup -eq '<DN-of-mailflow-watched-group>'"

Set-ManagementRoleAssignment -Identity "mailflow-mail-read" `
  -CustomResourceScope "mailflow-watched"
```
Verify: add/remove a user from the group, then confirm a subscription create (Phase 7) succeeds/fails accordingly.

- [ ] **Step 4 (CRITICAL): remove the broad Entra grant so scoping takes effect**

The effective permission is the **union** of Entra + Exchange RBAC. If the unscoped Entra `Mail.Read` stays, it **wins** and your scope does nothing.
```bash
# remove the tenant-wide app-role assignment granted in Phase 2
ASSIGNMENT_ID=$(az rest --method GET \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_OBJECT_ID/appRoleAssignments" \
  --query "value[?appRoleId=='$MAIL_READ_ROLE_ID'].id | [0]" -o tsv)
az rest --method DELETE \
  --url "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_OBJECT_ID/appRoleAssignments/$ASSIGNMENT_ID"
```
Verify: re-running the appRoleAssignments query returns no `Mail.Read` entry. **Wait 30 min–2 h for propagation**, then confirm: a `GET /users/<unwatched-mailbox>/messages` returns **403**, while the watched mailbox returns **200**.

> ⚠️ Order matters: do Step 4 only **after** the RBAC scope (Steps 2/3) is in place, or the app loses all mail access in the gap.

---

## Phase 7: Smoke test — prove a real subscription delivers to the hub

- [ ] **Step 1: Acquire an app-only token**

```bash
TOKEN=$(curl -s -X POST "https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/token" \
  -d "client_id=${APP_ID}" \
  -d "client_secret=<THE_SECRET>" \
  -d "scope=https://graph.microsoft.com/.default" \
  -d "grant_type=client_credentials" | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
echo "${TOKEN:0:12}…"
```
Verify: a non-empty token prints. (If using a cert, mint the token with MSAL instead.)

- [ ] **Step 2: Confirm the app can read the test mailbox**

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/users/${TEST_MAILBOX}/messages?\$top=1&\$select=id,subject" \
  | python -m json.tool
```
Expected: HTTP 200 with one message. A `403` means RBAC scope/propagation isn't ready (Phase 6).

- [ ] **Step 3: Create the Event Hubs subscription for Inbox**

```bash
EXPIRY=$(python -c "import datetime;print((datetime.datetime.utcnow()+datetime.timedelta(minutes=8640)).strftime('%Y-%m-%dT%H:%M:%SZ'))")
curl -s -X POST "https://graph.microsoft.com/v1.0/subscriptions" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "{
    \"changeType\": \"created,updated\",
    \"resource\": \"users/${TEST_MAILBOX}/mailFolders('inbox')/messages\",
    \"notificationUrl\": \"${NOTIFICATION_URL}\",
    \"lifecycleNotificationUrl\": \"${NOTIFICATION_URL}\",
    \"clientState\": \"mailflow\",
    \"expirationDateTime\": \"${EXPIRY}\"
  }" | python -m json.tool
```
Expected: HTTP **201** with a subscription `id`. Common failures:
- `400 … notificationUrl … validation` → the Change Tracking `Data Sender` role (Phase 3 Step 4) isn't applied or the `tenantId` domain is wrong.
- `403` over-limit / scope → Phase 6.
**Record:** the subscription `id` (for renew/delete).

- [ ] **Step 4: Observe events on the hub**

Send a test email to `$TEST_MAILBOX`, then check the hub received traffic:
```bash
az monitor metrics list --resource "$EH_SCOPE" \
  --metric "IncomingMessages" --interval PT1M --query "value[].timeseries[].data[].total" -o tsv
```
Expected: a non-zero count appears within ~1–3 min (Graph message latency). You should also see one **validation** event (`subscriptionId:"NA"`) right after Step 3 — your consumer ignores it.
Verify the payload shape with a quick consumer (optional):
```bash
pip install azure-eventhub
python - <<'PY'
import os
from azure.eventhub import EventHubConsumerClient
from azure.identity import DefaultAzureCredential
ns=os.environ["EH_NAMESPACE"]; hub=os.environ["EH_HUB"]; cg=os.environ["EH_CONSUMER_GROUP"]
c=EventHubConsumerClient(f"{ns}.servicebus.windows.net", hub, cg, credential=DefaultAzureCredential())
def on_event(ctx,e):
    print("EVENT:", e.body_as_str()[:300]); ctx.update_checkpoint(e)
with c:
    c.receive(on_event=on_event, starting_position="-1", max_wait_time=30)
PY
```
Expected: prints a Graph `changeNotificationCollection` JSON containing `resource: users/<id>/messages/<id>` and your `clientState: "mailflow"`.

- [ ] **Step 5: Clean up the test subscription (optional)**

```bash
curl -s -X DELETE "https://graph.microsoft.com/v1.0/subscriptions/<SUBSCRIPTION_ID>" \
  -H "Authorization: Bearer $TOKEN" -w "%{http_code}\n"
```
Expected: `204`.

---

## Phase 8: Scale to org-level (multi-user) subscriptions

- [ ] **Step 1: Decide the watched-mailbox source** (one, then expand):
  - test: the single `$TEST_MAILBOX`;
  - org: members of the `mailflow-watched` group (Phase 6 Step 3).

- [ ] **Step 2: Create per-mailbox × folder subscriptions in a loop** (Inbox + Sent)

```bash
for MBX in $(az rest --method GET \
   --url "https://graph.microsoft.com/v1.0/groups/<GROUP_ID>/members?\$select=mail" \
   --headers "Authorization=Bearer $TOKEN" --query "value[].mail" -o tsv); do
  for FOLDER in inbox sentitems; do
    curl -s -X POST "https://graph.microsoft.com/v1.0/subscriptions" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d "{\"changeType\":\"created,updated\",
           \"resource\":\"users/${MBX}/mailFolders('${FOLDER}')/messages\",
           \"notificationUrl\":\"${NOTIFICATION_URL}\",
           \"lifecycleNotificationUrl\":\"${NOTIFICATION_URL}\",
           \"clientState\":\"mailflow\",
           \"expirationDateTime\":\"${EXPIRY}\"}" \
      -w " [%{http_code}] ${MBX}/${FOLDER}\n" -o /dev/null
    sleep 0.2   # jitter — stay under throttling
  done
done
```
Verify: `az monitor metrics list --resource "$EH_SCOPE" --metric IncomingMessages …` rises as those mailboxes receive mail.
> All these subscriptions deliver to the **same** hub — the consumer side does not multiply. This loop is what mailflow's `SubscriptionReconciler` (plan `2026-06-11-mailflow-graph-eventhubs-adapter.md`, future task) automates: desired = mailboxes×folders, actual = `GET /subscriptions`, then create/renew/delete to match, hourly.

- [ ] **Step 3: Schedule renewal** — every ~hour, `PATCH /subscriptions/{id}` to extend `expirationDateTime`; recreate on `404`. (Implemented by `GraphSubscriptionManager`.)

---

## Values to hand to mailflow config (summary)

| mailflow config | Value from this runbook |
|---|---|
| `GraphConfig.tenant_id` | `$TENANT_ID` |
| `GraphConfig.client_id` | `$APP_ID` (Phase 1) |
| `GraphConfig.client_secret_ref` | secret-store ref (Phase 5) |
| `GraphConfig.mailboxes` | `$TEST_MAILBOX` then group members |
| `GraphConfig.folders` | `["inbox","sentitems"]` |
| `EventHubConfig.namespace` | `$EH_NAMESPACE` |
| `EventHubConfig.hub` | `$EH_HUB` |
| `EventHubConfig.consumer_group` | `$EH_CONSUMER_GROUP` |
| `EventHubConfig.tenant_domain` | `$TENANT_DOMAIN` |
| checkpoint store | `$STORAGE_NAME` / `$CHECKPOINT_CONTAINER` |

---

## Verification checklist (definition of done)

- [ ] `az ad app permission list` shows `Mail.Read` and admin consent applied.
- [ ] Change Tracking SP has **Data Sender** on the namespace.
- [ ] `EventHub:` notificationUrl created a subscription → **201**.
- [ ] A test email produced **IncomingMessages > 0** on the hub within ~3 min.
- [ ] A quick consumer printed a notification with `clientState: "mailflow"`.
- [ ] After RBAC scope + Entra-grant removal: watched mailbox → **200**, unwatched → **403**.
- [ ] (Org) the loop created Inbox+Sent subscriptions for all group members.

---

## Rollback / teardown

```bash
# delete all subscriptions the app created
for ID in $(curl -s -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/subscriptions" | python -c "import sys,json;[print(s['id']) for s in json.load(sys.stdin)['value']]"); do
  curl -s -X DELETE "https://graph.microsoft.com/v1.0/subscriptions/$ID" -H "Authorization: Bearer $TOKEN" -o /dev/null
done
# remove Azure resources
az group delete --name "$RG" --yes --no-wait
# remove the app registration (Entra)
az ad app delete --id "$APP_ID"
```

---

## Open questions to confirm with the admins (ask before running)

1. **Tenant + test mailbox**: exact `TENANT_ID`, `TENANT_DOMAIN`, and the first `TEST_MAILBOX`.
2. **Secret vs certificate** for the app credential (cert recommended for prod).
3. **Mailbox scope**: which **group** defines org-level watched mailboxes?
4. **Consumer identity**: will mailflow run as a **managed identity** (preferred) or an SP? (Drives Phase 4 Step 2.)
5. **Sovereign cloud?** Commercial vs GCC High/China changes base URLs and the Event Hub host suffix.

---

## Sources
- Outlook change notifications — per-mailbox resource paths, `Mail.Read`, no `.Shared`, 1000/mailbox — https://learn.microsoft.com/en-us/graph/outlook-change-notifications-overview
- Event Hubs delivery — `EventHub:` notificationUrl, Data Sender role, ignore "NA" validation — https://learn.microsoft.com/en-us/graph/change-notifications-delivery-event-hubs
- RBAC for Applications — scoping, union gotcha, remove Entra grant — https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac
- Subscription create/renew/delete + lifetimes — https://learn.microsoft.com/en-us/graph/api/resources/subscription
