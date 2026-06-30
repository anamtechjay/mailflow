# Microsoft Graph (Outlook / Exchange Online) connection — concepts & design

> **Status:** research / design. No code yet. Every limit, permission name, and
> resource path below was verified against the official Microsoft Graph docs on
> **learn.microsoft.com** (pages updated April 2026; see [Sources](#sources)).
> Where this contradicts earlier internal notes, the docs win and the difference
> is **flagged** inline.
>
> **Scope:** this is the FIRST mailflow provider adapter — Graph, **app-only**.
> The mailflow core stays vendor-free; everything here lives in `adapters/graph/`.
> Gmail later implements the SAME ports. mailflow's job ends at emitting a
> `CleanEmail` event — storage / AI / UI belong to the consuming app.

---

## 0. TL;DR corrections to prior notes (read first)

| Claim in the task brief / earlier notes | Verified truth (learn.microsoft.com, Apr 2026) | Action |
|---|---|---|
| "Message subscription max is **~4230 min / ~2.94 days**, NOT 7 days" | **WRONG for `message`.** Outlook **message/event/contact** max expiry = **10,080 minutes (under 7 days)** for *basic* subscriptions; **1,440 minutes (under 1 day)** for *rich (resource-data)* subscriptions. The `4,230 min` figure belongs to *other* resources (callRecord, group conversation, printer, onlineMeeting, todoTask). | Use **10,080 min** (basic) / **1,440 min** (rich) for our mail subscriptions. |
| Webhook must echo `validationToken` "within 10 seconds" | **Correct.** 200 OK, `text/plain`, URL-decoded plain text, **≤10 s**. | Keep. |
| Notification response "503 (retry) vs 202 (stop)" | **Correct in spirit.** Any **2xx within 3 s** = delivered (stop). **non-2xx or timeout** = retried up to **4 hours** (retry timeout extended to 10 s). Recommended: **202 Accepted**; return **5xx** to force retry. | Use 202 on success, 5xx on transient failure. |
| EWS retires "Oct 2026" | **Correct, phased.** Disablement **starts Oct 2026**; **fully disabled Apr 2027**; tenants default `EWSEnabled=False` from **Aug 2026** unless allow-listed. Graph-only is the right direction. | No EWS. Graph only. |

---

## A. Identity & authentication (app-only)

### App registration (Microsoft Entra ID)
A single Entra **app registration** gives us three identifiers + one secret:
- **`tenant_id`** — the customer's Entra tenant (GUID).
- **`client_id`** (application id) — our app.
- **`client_secret`** OR (preferred for prod) a **certificate** (client-credentials cert).

> ⚠️ **Tenant-specific values — do NOT assume.** `tenant_id`, `client_id`, the
> secret/cert, and the watched mailbox addresses are all per-deployment and must
> come from the admin via the `SecretProvider` port. They appear here only as
> placeholders. *(I will ask before filling any in.)*

### App-only (client credentials) flow
- Library: **MSAL** (`msal.ConfidentialClientApplication`).
- Call: **`acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])`**.
- The **`.default`** scope tells Entra "use the application permissions already
  consented for this app" — you do **not** list individual scopes at runtime.
- **Token caching:** MSAL caches in-memory and returns a cached token until ~5 min
  before expiry, then transparently re-acquires. Tokens last ~60–90 min. Our
  adapter holds one `ConfidentialClientApplication` and asks per request; MSAL
  handles refresh. No refresh tokens exist in client-credentials (re-acquire).

### App-only vs delegated
| | App-only (we use this) | Delegated |
|---|---|---|
| Identity | The app itself (a service principal) | A signed-in user |
| Mailbox reach | **Any** mailbox in tenant: `users/{id}/...` | Only the signed-in user: `me/...` |
| Runtime consent | None (admin pre-consents once) | Per-user / admin consent |
| Fit | Unattended ingestion of many mailboxes | n/a for our use case |

We always address mailboxes as **`users/{id|userPrincipalName}/...`**, never `me/...`.

### Least privilege — scoping app-only to specific mailboxes
App-only `Mail.Read` is **tenant-wide by default** (it can read *every* mailbox).
To restrict it to an allow-list of mailboxes, layer **RBAC for Applications**
(Exchange Online) — see §B and the provisioning checklist. This is the single
most important security control for this adapter.

---

## B. Permissions & consent

### The permission we need
- **`Mail.Read`** — **Application** permission. Grants read of messages,
  mail folders, and is the resource permission that backs a **`/messages`
  change-notification subscription**. (Subscriptions require the same permission
  that would let you *read* the subscribed resource.)
- We do **not** need `Mail.ReadWrite`, `Mail.Send`, or any write scope — mailflow
  is read-only ingestion.

### Why NOT the `.Shared` variants
`Mail.Read.Shared` is a **delegated-only** concept (access to mailboxes shared
*with the signed-in user*). It is meaningless for app-only and would not scope us;
ignore it.

### Consent
- Application permissions require **admin consent** (a Global Admin grants
  `Mail.Read` once in the Entra portal / via `/adminconsent`). No per-user prompts.
- **Rich (encrypted) notifications gotcha:** if the app's service principal has
  `appRoleAssignmentRequired = true`, you must also assign the **Microsoft Graph
  Change Tracking** service principal (**appId `0bf30f3b-4a52-48df-9a82-234910c4a086`**)
  an app role — otherwise notifications arrive with a **`null` validationToken**.
  Recommended: set `appRoleAssignmentRequired = false`. (Only relevant if we choose
  rich notifications — see §D.)

---

## C. Connection config & secrets

```
GRAPH_API_BASE_URL = https://graph.microsoft.com/v1.0     # v1.0, never /beta in prod
authority          = https://login.microsoftonline.com/{tenant_id}
scope              = https://graph.microsoft.com/.default
```

- **Secrets never hardcoded.** `tenant_id` may be config; `client_secret` / cert /
  the rich-notification private key come through the **`SecretProvider` port**
  (`get(ref) -> str`). The adapter receives *references*, resolves them lazily.
- National clouds (GCC High, China) use different base URLs — out of scope for v1;
  flag if the tenant is sovereign.

---

## D. Subscriptions (the heart of it)

### Creating a subscription
`POST {base}/subscriptions` with:

| Field | Value for us | Notes |
|---|---|---|
| `changeType` | `"created,updated"` | new mail + edits/moves. (`deleted` optional.) |
| `resource` | `users/{id}/mailFolders('inbox')/messages` | **per mailbox + folder** |
| `notificationUrl` | our public HTTPS webhook | validated on create (§E) |
| `lifecycleNotificationUrl` | our public HTTPS lifecycle endpoint | **set at create — cannot be PATCHed in later** |
| `clientState` | a per-subscription secret (≤**128 chars**) | tripwire, never logged |
| `expirationDateTime` | now + chosen lifetime (see table) | under 45 min ⇒ auto-bumped to 45 min |
| `includeResourceData` | `false` (basic) — recommended v1 | `true` ⇒ rich/encrypted, +cert, shorter life |
| `encryptionCertificate` / `encryptionCertificateId` | only if rich | base64 X.509 **public** key |

Success ⇒ **`201 Created`** + a `subscription` object with a unique **`id`**.
- **Duplicate** (same `changeType`+`resource`) ⇒ **`409 Conflict`**.
- **Over per-mailbox limit** ⇒ **`403 Forbidden`** (message explains the limit).

### Folders we watch
Graph change notifications are **per folder** (the cursor/delta is per folder too).
Each watched folder = one subscription = one mailflow `StreamRef`.
- **DECIDED (v1):** **Inbox + Sent** — watch `mailFolders('inbox')` and
  `mailFolders('sentitems')` per mailbox (inbound + outbound capture). Custom
  folders deferred; the watched-folder list stays config-driven so adding them
  later is config-only.
- Trade-off: **Inbox-only** misses mail that server-side rules file elsewhere;
  **all-folders** maximizes coverage but multiplies subscriptions toward the
  **1,000-active-subscriptions-per-mailbox** cap (shared across *all* apps).
- Common folder paths: `mailFolders('inbox')`, `mailFolders('sentitems')`, or
  `mailFolders('{folderId}')` for custom folders.

### Basic vs Rich (encrypted) notifications — both covered

**Basic (recommended v1):** the notification carries only the message **id**. The
webhook does a follow-up `GET` (§F) using our own app token. Simpler; **7-day**
subscription life; and it fits the "trust nothing in the payload" rule because we
re-fetch authoritative data ourselves.

**Rich / resource-data (encrypted):** the notification embeds the (encrypted)
message. Saves the re-fetch round-trip, but:
- Subscription life drops to **1,440 min (1 day)** ⇒ more frequent renewal.
- Requires a **cert**: provide a base64 X.509 **public** key
  (`encryptionCertificate`), RSA **2,048–4,096 bit**; keep the **private** key in
  our `SecretProvider`.
- **Decryption chain** (per item, from `encryptedContent`):
  1. Pick the cert via `encryptionCertificateId`.
  2. RSA-decrypt `dataKey` with our private key using **OAEP** padding ⇒ single-use
     symmetric key.
  3. Verify integrity: **HMAC-SHA256** of `data` (keyed by the symmetric key) must
     equal `dataSignature`; mismatch ⇒ treat as tampered, do not decrypt.
  4. AES-decrypt `data`: **AES-256, CBC, PKCS7**, IV = **first 16 bytes** of the
     symmetric key ⇒ JSON of the resource.
- **Authenticity:** rich payloads include a **`validationTokens`** JWT array; each
  must be validated (issuer = Microsoft identity platform, audience = our
  `client_id`, and **`appid`/`azp` == `0bf30f3b-4a52-48df-9a82-234910c4a086`**).

> **DECIDED (v1):** **basic notifications + re-fetch.** Simpler, longer-lived
> (7-day sub), aligns with the "trust nothing in the payload" rule. Rich is a
> later optimization kept as a **config-only** switch (cert + decryption code
> designed but not wired).

### Subscription lifetime & renewal — verified table

| Subscription kind (Outlook message) | Max `expirationDateTime` | Our renewal cadence |
|---|---|---|
| **Basic** (`includeResourceData:false`) | **10,080 min ≈ 7 days** | renew every **~24 h** (margin well under max) |
| **Rich** (`includeResourceData:true`) | **1,440 min ≈ 1 day** | renew every **~6–12 h** |

- **45-minute floor:** any requested expiry < 45 min from now is auto-set to 45 min.
- **Renew:** `PATCH {base}/subscriptions/{id}` with a new `expirationDateTime`
  ⇒ `200 OK`. Renewal also refreshes the endpoint access token.
- **Recreate-on-404:** if a renewal `PATCH` returns **404**, the subscription is
  gone — **create a new one**, then **delta-sync the gap** (§H) so no mail is lost.
- **Delete:** `DELETE {base}/subscriptions/{id}` ⇒ `204`.

### Lifecycle notifications (sent to `lifecycleNotificationUrl`)
| `lifecycleEvent` | Meaning | Our reaction |
|---|---|---|
| `reauthorizationRequired` | Endpoint token about to expire, or admin revoked consent; delivery pauses | **Reauthorize** (`POST /subscriptions/{id}/reauthorize`) **or** renew (`PATCH`) — not both within 10 min — then **delta-sync** the gap |
| `subscriptionRemoved` | Graph dropped the subscription | **Create a new** subscription, then **delta-sync** |
| `missed` | Graph couldn't deliver some notifications | **Delta-sync / poll** the affected stream to recover |

> ⚠️ **Frozen-port gap (flag for the lead):** the real `core/ports.py`
> `SubscriptionManager` Protocol is only `ensure_watch` / `renew_watch`. It has
> **no lifecycle hook** (the solution doc's aspirational `on_lifecycle` was never
> built into core). The Graph adapter needs to handle the three lifecycle events;
> options: (a) extend the core port (needs lead sign-off — it's a frozen contract),
> or (b) keep lifecycle handling **adapter-internal** (a `GraphLifecycleHandler`
> the webhook calls) and only expose `ensure_watch`/`renew_watch` through the port.
> **Recommend (b)** for v1 to avoid touching the frozen core.

### Limits
- **1,000 active subscriptions per mailbox**, shared across **all** apps. With a
  multi-folder policy across many mailboxes, budget against this.
- **45-min floor**, **409** on duplicate, **403** over-limit (above).
- Webhook endpoint **slow/drop throttling** — see §E.

### Notification latency (plan SLAs around this)
`message`: **avg < 1 min, max 3 min** between the mailbox event and delivery.

---

## E. Notification handling (the webhook)

**Golden rule: the webhook does ZERO Graph calls.** It validates, extracts ids,
publishes a lightweight pointer to the transport, and returns fast. The heavy
`GET` happens downstream (mailflow's fetch/extract stages), off the hot path.

### Validation handshake (subscription create)
Graph POSTs `…?validationToken={opaque}` (`Content-Type: text/plain`). We must,
**within 10 s**, return **`200 OK`**, **`text/plain`**, body = the **URL-decoded**
token verbatim (no HTML/JSON encoding). If we also set `lifecycleNotificationUrl`,
**both** endpoints get a validation request (two handshakes if same URL).

### Normal notification response contract
- Return **2xx within 3 s** ⇒ delivered (Graph stops).
- **non-2xx or >3 s** ⇒ Graph **retries up to 4 hours** (retry timeout 10 s),
  exponential backoff.
- **Recommended:** persist to the transport/queue, return **`202 Accepted`** fast;
  return **`5xx`** only when we genuinely couldn't queue it (forces retry).
- **Endpoint health throttling (avoid this):**
  - **"slow"** if >10% of responses exceed 3 s in a 10-min window ⇒ new
    notifications delayed 10 min.
  - **"drop"** if >15% exceed 10 s ⇒ notifications **dropped** (unrecoverable) for
    10 min. ⇒ keep the handler trivial and async.

### Authenticity (basic notifications)
- Compare **`clientState`** in constant time to the value we set; mismatch ⇒
  discard + investigate. **Never log `clientState`**; strip it before storing.
- Dedupe replays on **`(subscriptionId, resourceData.id)`** for a window.
- (Rich notifications: also validate the `validationTokens` JWTs — §D.)

### Basic notification payload shape (what the webhook parses)
```json
{ "value": [ {
  "subscriptionId": "…",
  "subscriptionExpirationDateTime": "2026-…Z",
  "clientState": "…",
  "changeType": "created",
  "resource": "users/{userId}/messages/{messageId}",
  "tenantId": "…",
  "resourceData": { "@odata.type":"#Microsoft.Graph.Message",
                    "@odata.id":"Users/{…}/Messages/{…}",
                    "id":"{messageId}" }
} ] }
```
We extract **`{userId, messageId, changeType}`** ⇒ emit a pointer to the transport.

---

## F. Fetching the actual message

When a pointer is processed, `GET` the message with an explicit `$select` to keep
payloads small and predictable:

```
GET {base}/users/{userId}/messages/{messageId}?$select=
    id,internetMessageId,subject,from,sender,toRecipients,ccRecipients,bccRecipients,
    replyTo,body,uniqueBody,bodyPreview,isRead,isDraft,importance,hasAttachments,
    receivedDateTime,sentDateTime,createdDateTime,conversationId,conversationIndex,
    parentFolderId,internetMessageHeaders,categories
```

- **`body` vs `uniqueBody`:** `body` is the full thread/content (HTML or text per
  `body.contentType`); **`uniqueBody`** is *just the latest message's* contribution
  (great for snippet/relevance and avoiding quoted-thread noise). Fetch both; map
  `body`→`body_html`/`body_text`, keep `uniqueBody` for snippet/de-dup.
- **`internetMessageHeaders`** must be explicitly `$select`ed; it yields the raw
  RFC headers we need for `In-Reply-To`, `References`, `List-Id`,
  `List-Unsubscribe`, `Auto-Submitted`. ⚠️ *Open question:* Graph has historically
  capped/truncated this collection — **verify completeness at build time**.
- **Fetch-by-`internetMessageId` fallback (folder-move resilience):** Graph
  message **`id` changes when a message moves folders** (server rules, user
  filing). If a `GET {messageId}` 404s, fall back to
  `GET {base}/users/{userId}/messages?$filter=internetMessageId eq '{rfcId}'`
  — `internetMessageId` is stable across moves. This is also why our **dedupe key
  is `internetMessageId`**, not the Graph `id` (§H).
- **`parentFolderId`** is an opaque id, not a name ⇒ resolve to a folder display
  name via `GET {base}/users/{userId}/mailFolders/{parentFolderId}?$select=displayName`
  (cache the id→name map per mailbox).

---

## G. Attachments

- **List:** `GET {base}/users/{userId}/messages/{messageId}/attachments` (returns
  `@odata.type`, `name`, `contentType`, **`size`**, `isInline`, `contentId`).
- **Types:**
  - `#microsoft.graph.fileAttachment` — has `contentBytes` (base64) for small
    files; fetch raw bytes via **`/attachments/{id}/$value`** for large ones.
  - `#microsoft.graph.itemAttachment` — an embedded Outlook item (message/event);
    needs `?$expand=microsoft.graph.itemAttachment/item`.
  - `#microsoft.graph.referenceAttachment` — a link (OneDrive/SharePoint), no bytes.
  - **Inline** (`isInline:true` / has `contentId`) — logos, tracking pixels;
    classify as inline media, not real attachments (mirrors the MIME extractor).
- **TNEF (`winmail.dat`):** legacy Outlook RTF encapsulation. Graph usually
  surfaces the decoded parts, but a raw `winmail.dat` can still appear ⇒ treat as a
  normal (possibly undecodable) attachment; do not attempt TNEF parsing in v1.
- **Size thresholds (verify at build, but per docs/notes):** use `contentBytes`
  under **~3 MB**; stream **`/$value`** at/above **~3 MB**; Graph caps request
  bodies near **~4 MB**, and attachments can reach **150 MB**. Stream large bytes
  straight to the **`BlobStore`** port; emit a **`storage_ref`**, never the bytes.
- **Size guard:** read the per-attachment **`size`** *before* download and route
  oversized items to DLQ without fetching bytes (mailflow §8.6). Base64 inflates
  wire size ~33% — budget headroom.
- **Throttling:** honor **`429` + `Retry-After`** on attachment fetches (§H).

---

## H. Reliability & state

### Catch-up: delta query vs watermark
Change notifications are **best-effort** (the `missed` lifecycle event exists for a
reason). We need a durable catch-up:
- **Preferred: delta query** — `GET {base}/users/{userId}/mailFolders/{folderId}/messages/delta`,
  persist the returned **`deltaLink`/`deltaToken`** as the mailflow **`Cursor`**
  (per folder). On notification-gap / `missed` / startup, replay from the
  `deltaToken` to get exactly what changed. This is the natural fit for mailflow's
  per-stream monotonic cursor.
- **Fallback: `receivedDateTime` watermark** — `…/messages?$filter=receivedDateTime gt {ts}`.
  Simpler but coarser (misses edits, can double-deliver near the boundary). Use
  only if delta is unavailable for a folder.

### Cursor / dedupe / idempotency (maps cleanly to mailflow)
- **`CursorStore`** holds the per-folder `deltaToken` (mailflow `Cursor.value`),
  advancing monotonically.
- **Dedupe key = `internetMessageId`** (stable across folder moves) folded into
  mailflow's idempotency key `(tenant, mailbox, provider_message_id)`. ⚠️ Graph
  `id` is **not** stable — never dedupe on it.
- **Missed-notification sweep:** a periodic delta poll per stream catches anything
  the webhook never delivered; the cursor makes it idempotent.

### Throttling (Graph platform limits — verify at integration)
- Per notes/citations: **~10,000 requests / 10 min** and **~4 concurrent per
  app+mailbox**; **429 + `Retry-After`** ⇒ honor the header, exponential backoff.
- **Don't stampede:** cap how many mailboxes re-sync at once per tenant (a mass
  resync can trip the limit).
- **Batch** independent reads with `$batch` (up to 20 requests) where it helps.

### Message size probe (mailflow §8.6 guard) — ⚠️ open question
The **v1.0 `message` resource has NO `size` field.** Options:
1. **`PidTagMessageSize` extended property** — community-documented, *not* on the
   official v1.0 message page ⇒ **verify against the SDK before relying on it**.
2. **Sum per-attachment `size`** (documented) + body length as an approximation.
3. `mailboxItem.size` exists in **beta** only.
v1 recommendation: enforce the byte ceiling against **attachment `size` metadata**
(documented) and skip a true whole-message probe until #1 is verified.

---

## I. Migration note (EWS)

**EWS is retiring** (Exchange Online only; Exchange Server unaffected):
- **Aug 2026:** tenants default to `EWSEnabled=False` unless explicitly allow-listed.
- **Oct 2026:** phased global disablement **begins**.
- **Apr 2027:** EWS **fully disabled**.
Microsoft Graph has near-complete parity and is the sanctioned path ⇒ **Graph-only
is correct**; we never touch EWS or the deprecated Application Impersonation role.

---

## J. Mapping to mailflow ports

### Ports the Graph adapter **implements**
| mailflow port (`core/ports.py`) | Graph class | Responsibility |
|---|---|---|
| **`MailboxProvider`** (`connect`, `sync_streams`, `fetch`, `message_size`) | `GraphProvider` | MSAL connect; enumerate watched mailbox×folder streams; `fetch` = delta query → `RawMessage`s; `message_size` from attachment metadata |
| **`SubscriptionManager`** (`ensure_watch`, `renew_watch`) | `GraphSubscriptionManager` | create/renew subscriptions; recreate-on-404. Lifecycle events handled **adapter-internally** (see §D gap) |
| **`EnvelopeParser`** (`parse_envelope`) | `GraphEnvelopeParser` | Graph message **JSON → `Envelope`** — no MIME parse needed (Graph gives structured fields + `bodyPreview`) |
| **`ContentExtractor`** (`extract(msg, env)`) | `GraphExtractor` | full Graph JSON (+attachments) → `CleanEmail`. **Implements the port `extract(msg, env)` directly** — this is exactly why the pipeline's extractor seam exists (`MimeExtractor.extract_bytes` vs port `extract`; see `CLAUDE.md` frozen contracts) |

### Infra ports the adapter **consumes** (does not implement)
`CursorStore` (deltaTokens) · `DedupeStore` (internetMessageId) · `SecretProvider`
(client secret/cert + rich private key) · `BlobStore` (attachment bytes) ·
`Emitter` (the transport the webhook publishes pointers to / the pipeline emits to).

### Webhook + transport (outside the ports, adapter-owned)
A thin HTTPS app (`GraphWebhook`) for `notificationUrl` + `lifecycleNotificationUrl`:
validate → extract ids → publish pointer → 202. It is **not** a mailflow port; it
feeds the provider via the transport.

---

## K. Graph message JSON → `CleanEmail` field mapping

| `CleanEmail` field | Graph source | Notes |
|---|---|---|
| `provider` | `"graph"` (constant) | |
| `provider_message_id` | `id` | **not** stable across folder moves |
| `provider_stream_id` | `{mailbox}:{folderId}` | the `StreamRef.key` |
| `message_id` / `message_id_present` / `message_id_trusted` | `internetMessageId` | feeds `derive_canonical_id` (trusted iff `<local@domain>`) |
| `canonical_id` | from `internetMessageId` else `stable_hash` | mailflow identity rule (§6.1) |
| `in_reply_to` | `internetMessageHeaders[In-Reply-To]` | header must be `$select`ed |
| `references` | `internetMessageHeaders[References]` | split on whitespace |
| `subject` | `subject` | |
| `from_` (alias `from`) | `from.emailAddress {name,address}` | author |
| `sender` | `sender.emailAddress` | sending mailbox (differs on send-on-behalf) |
| `reply_to` | `replyTo[0].emailAddress` | |
| `to` / `cc` / `bcc` | `toRecipients[]` / `ccRecipients[]` / `bccRecipients[]` | |
| `body_html` / `body_text` | `body.content` by `body.contentType` (`html`/`text`) | prefer text; fall back to HTML |
| `body_truncated` | set if we cap body length | |
| `snippet` (Envelope) | `bodyPreview` or `uniqueBody` | free from Graph |
| `date_utc` | `sentDateTime` | |
| `received_at` | `receivedDateTime` | |
| `is_draft` | `isDraft` | |
| `direction` | compare `from.address` to watched mailbox | inbound/outbound |
| `attachments[]` | `/attachments` list → `Attachment` | `name`→filename, `contentType`, `size`→size_bytes, `isInline`→is_inline, `contentId`→content_id, `id`→provider_attachment_id, blob ptr→storage_ref, sha256→content_hash |
| `categories` | `categories[]` | Graph categories (empty for Gmail) |
| `labels` | — | Gmail-only; empty for Graph |
| `folder` | resolve `parentFolderId` → displayName | one folder (Graph) |
| `list_id` / `list_unsubscribe` | `internetMessageHeaders[List-Id/List-Unsubscribe]` | |
| `auto_submitted` | `internetMessageHeaders[Auto-Submitted]` | filters use this |
| `raw_headers` | `internetMessageHeaders[]` → `{lower(name): [values]}` | ⚠️ may be truncated by Graph |
| `message_size_bytes` | Σ attachment `size` (+ body) or `PidTagMessageSize` | no `size` on v1.0 message (§H) |
| `relevance` / `matched_filter` | set by pipeline, not the adapter | |
| `schema_version` | `"1.3"` | |

---

## L. Open questions — confirm with the admin / at build time

1. **Tenant values (BLOCKER):** `tenant_id`, `client_id`, secret-vs-certificate
   choice, and the **exact list of mailboxes** to watch. *(Will ask before coding.)*
2. ~~**Folder policy**~~ — **DECIDED: Inbox + Sent** (config-driven; custom folders deferred).
3. ~~**Notification mode**~~ — **DECIDED: basic + re-fetch** (rich kept as config-only switch).
4. **RBAC scoping:** can the Exchange admin create an **RBAC for Applications**
   management scope limiting the app to the mailbox group, AND remove any broad
   Entra `Mail.Read` grant? (The broad grant *wins* if left in place; changes take
   **30 min–2 h** to propagate.)
5. **Webhook URL:** the public HTTPS endpoint must be reachable *before* the first
   subscription create (handshake). Who provisions it / TLS / IP-allowlist Graph?
6. **`internetMessageHeaders` completeness:** verify Graph returns the full header
   set we need (In-Reply-To/References/List-*) without truncation.
7. **Message size:** is `PidTagMessageSize` reliable via the SDK, or do we settle
   for attachment-size-based guarding in v1?
8. **`SubscriptionManager` lifecycle gap:** lead decision — extend the frozen core
   port, or keep lifecycle handling adapter-internal (recommended).
9. **Sovereign cloud?** Is the tenant commercial or GCC High/China (different base
   URLs)?

---

## M. Microsoft-side provisioning checklist (must exist before any code runs)

- [ ] **Entra app registration** created; record `tenant_id`, `client_id`.
- [ ] **`Mail.Read` (Application)** permission added **+ admin consent granted**.
- [ ] **Client secret or certificate** issued; stored in the secret manager
      (referenced via `SecretProvider`, never in code).
- [ ] **RBAC for Applications** management scope limiting the app to the watched
      mailbox group **+ the unscoped Entra grant removed** (allow 30 min–2 h).
- [ ] **Public HTTPS webhook** (`notificationUrl`) + **lifecycle** endpoint live,
      TLS ≥1.2, reachable for the validation handshake.
- [ ] *(Rich only)* **RSA 2048–4096 cert**: public key for the subscription,
      **private key** in the secret manager; service principal
      `appRoleAssignmentRequired=false` (or Change-Tracking app-role assigned).
- [ ] **Secret store** (Key Vault / equivalent) reachable by the adapter.
- [ ] Confirm tenant is **commercial** (not sovereign) or adjust base URLs.

---

## Sources

- Subscription resource & properties — https://learn.microsoft.com/en-us/graph/api/resources/subscription
- Change notifications overview (supported resources, lifetimes, latency, limits) — https://learn.microsoft.com/en-us/graph/change-notifications-overview
- Webhook delivery (validation handshake, 3 s/10 s, retry, slow/drop) — https://learn.microsoft.com/en-us/graph/change-notifications-delivery-webhooks
- Rich/encrypted notifications (cert, OAEP, AES-256-CBC, HMAC, validationTokens, appId `0bf30f3b-…`) — https://learn.microsoft.com/en-us/graph/change-notifications-with-resource-data
- Lifecycle notifications — https://learn.microsoft.com/en-us/graph/change-notifications-lifecycle-events
- EWS retirement timeline — https://techcommunity.microsoft.com/blog/exchange/exchange-online-ews-your-time-is-almost-up/4492361 · https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-ews-exchange-online
- (To verify at build) Attachments/large files, throttling, RBAC for Applications, message-size extended property — see learn.microsoft.com `/graph/outlook-large-attachments`, `/graph/throttling-limits`, `/exchange/permissions-exo/application-rbac`, `/graph/api/resources/message`
