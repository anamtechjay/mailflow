# mailflow — Project Q&A Study Guide

A self-study + tech-lead interview guide for the **mailflow** email-ingestion library.
Use it two ways:
- **✅ answers** — grounded in the code; you can say these confidently when demoing/explaining.
- **🔶 Ask lead** — a decision, business reason, or ops detail you should confirm with your tech lead. My best guess is included so you know *why* you're asking.

> One-line pitch: *mailflow connects to an inbox (Gmail today), normalizes every message into a provider-neutral `CleanEmail`, and hands it to your app — the app owns storage; mailflow only keeps a tiny cursor + dedupe bookkeeping.*

---

## Section 1 — Big Picture & Architecture (20)

1. **What problem does mailflow solve?** ✅ It ingests email from any provider and gives the app one clean, normalized object (`CleanEmail`) so the app never deals with raw MIME, OAuth, or provider quirks.
2. **What is mailflow NOT responsible for?** ✅ Storing emails. The app owns storage; mailflow keeps only a cursor (where it left off) and dedupe records.
3. **What architecture style is this?** ✅ Hexagonal / ports-and-adapters. A "core spine" depends only on Protocol *ports*; vendor code lives in *adapters*.
4. **Where are the ports defined?** ✅ `src/mailflow/core/ports.py` — Protocols like `MailboxProvider`, `Emitter`, `CursorStore`, `DedupeStore`, `BlobStore`.
5. **What is the "core spine"?** ✅ `core/pipeline.py` (orchestrator) + models + ports + identity + filtering + observability. It has zero vendor imports.
6. **What's the public entry point?** ✅ `connect(provider, ...)` in `facade.py`, returning a `Mailflow` handle. Also exported from the package root `__init__.py`.
7. **What providers exist?** ✅ `memory` (test/batch), `gmail` (live, full), `graph`/Outlook (coded but **not exposed** in `connect()` yet).
8. **Why is Graph built but hidden?** 🔶 Ask lead — likely it's not validated against a real Outlook/EventHubs tenant yet; our org email is Google Workspace so it can't be tested end-to-end here.
9. **What language/runtime/tooling?** ✅ Python ≥3.12, pydantic 2, mypy `--strict`, pytest. Built with hatchling.
10. **What are the hard dependencies?** ✅ Only `pydantic`. Provider SDKs are optional extras (`[gmail]`, `[graph]`) and lazy-imported.
11. **Why lazy-import provider SDKs?** ✅ So `import mailflow` works with no cloud SDKs installed; you only need extras for the provider you actually use.
12. **What is a `CleanEmail`?** ✅ The normalized output model (`core/models.py`): envelope fields, body text/html, attachments, thread key, labels, `canonical_id`, `schema_version`.
13. **What is `canonical_id`?** ✅ Stable identity: the trusted `<...@...>` Message-ID if present, else `stable_hash(provider, provider_message_id, mailbox)`.
14. **What is `idempotency_key`?** ✅ `(tenant, mailbox, provider_message_id)` — used for dedupe/claiming.
15. **What's the current schema version?** ✅ Schema `1.3` (added stripped-attachment support). `SCHEMA_VERSION` lives in `core/events.py`.
16. **How big is the codebase?** ✅ ~6,700 lines of source, 402 test functions across 66 test files.
17. **What does the `Mailflow` handle let me do?** ✅ Pick ONE style: `stream()` (pull loop), `run(on_email=...)` (push callback), or `fetch_new()` (batch one-pass). Plus `get_email(id)`, `health()`.
18. **What is the `tenant` concept?** ✅ A namespace string (default `"default"`) scoping cursors/dedupe so one deployment can serve multiple logical tenants.
19. **Is it sync or async?** ✅ V1 is strictly synchronous, one message at a time (`SYNC_ONLY = True` in ports). Async would be additive ports, never a breaking change.
20. **What's the project's maturity / version?** ✅ `0.1.0`, described as "core spine." 🔶 Ask lead for the productization roadmap (the plan is to ship it as an installable library, Gmail+memory first, SaaS later).

---

## Section 2 — The Processing Pipeline, Step by Step (20)

The per-message order (`core/pipeline.py`): **claim → size-guard → parse → filter → (classify) → extract → emit → mark-done.** Each step maps to a numbered spec invariant.

1. **What kicks off processing?** ✅ `Pipeline.run_once()` → `provider.connect()` → loop over `sync_streams()` → `fetch(stream, cursor)` per stream.
2. **What is a "stream"?** ✅ A `StreamRef` — a mailbox (Gmail) or mailbox+folder (Graph). One cursor per stream.
3. **Step 1 — claim. What happens?** ✅ `dedupe_store.try_claim(key, lease)` runs *before any spend*. If it fails, the message is a duplicate and is recorded/skipped.
4. **Why claim before doing work?** ✅ Prevents two runs (e.g. overlapping push + sweep) from processing the same message twice.
5. **Step 2 — size guard. What's the rule?** ✅ Check reported size *before* downloading bytes. Default cap 50 MB. **Fail-closed**: unknown/zero size is treated as over-limit and dead-lettered.
6. **Why fail closed on unknown size?** ✅ We can't vouch a message is within budget, so we never download it — protects memory/cost.
7. **Step 3 — parse. What does it produce?** ✅ A cheap, provider-neutral `Envelope` (headers, from/to, subject) used by filters *before* the heavy body extraction.
8. **Step 4 — filter. What are the outcomes?** ✅ Three-valued `Decision`: `keep`, `drop`, `uncertain` (`core/models.py`).
9. **What happens on `drop`?** ✅ `mark_done`, record a `dropped` trace, cursor advances. No extraction, no emit.
10. **What happens on `uncertain`?** ✅ If a `Classifier` is wired, it runs to score relevance; otherwise the email proceeds to extract.
11. **Step 5 — extract. What's the seam here?** ✅ `MimeExtractor.extract_bytes(raw, ...)` for raw RFC822; the generic `extract(msg, env)` port for adapters like Graph. `_extract` dispatches by type.
12. **Where do attachment bytes go?** ✅ Into the `BlobStore` (`storage_ref` pointer on the `Attachment`), never inline in the `CleanEmail`.
13. **Step 6 — clean. Optional?** ✅ Yes — a `ContentCleaner` (default `ThinContentCleaner`) does gentle HTML→text. Swappable / can be disabled.
14. **Step 7 — emit. What goes on the wire?** ✅ An `EmailEvent(schema_version, tenant, ordering_key, idempotency_key, email)` to the configured `Emitter`.
15. **Step 8 — mark-done. Why last?** ✅ `mark_done` is irreversible; it runs only after a successful emit so a crash mid-way leaves the message reclaimable.
16. **When does the cursor advance?** ✅ On ANY terminal disposition: emitted, dropped, duplicate, dead_lettered. Monotonic, single-writer (`commit_if_ahead`).
17. **What are the four terminal dispositions?** ✅ `emitted`, `dropped`, `duplicate`, `dead_lettered` (`Disposition` enum).
18. **What happens to a poison/oversized message?** ✅ It's dead-lettered, and the cursor steps past it so the stream isn't stuck — exactly one DLQ count per message.
19. **How are retries bounded?** ✅ `max_attempts` (default 3). `record_attempt` counts; transient/unknown errors `release()` the claim and retry, then DLQ.
20. **What does a `RunReport` give me?** ✅ Counts + decision traces (fetched, emitted, dropped, duplicate, dead_lettered, stripped) — the observability output of one pass.

---

## Section 3 — Functionality & Features (20)

1. **How do I pull emails in a loop?** ✅ `mf = connect("gmail", ...); for email in mf.stream(): ...`
2. **How do I push to a callback?** ✅ `connect("gmail", ..., on_email=handle); mf.run()` (blocking push loop).
3. **How do I batch-fetch once?** ✅ `connect("memory", seed=...).fetch_new()` returns the list in one pass.
4. **How do I fetch a single email by ID?** ✅ `mf.get_email(message_id)` (also `get_body`, `get_recipients`, `get_attachments`). Live providers only.
5. **What are `filters`?** ✅ A unified list passed to `connect(filters=[...])`: built-in dict specs, plain functions, or `Filter` objects (`normalize_filters`).
6. **What built-in filters exist?** ✅ Deterministic filters (to/cc, sender, etc.) in `filters/deterministic.py`; the docs/explorer lists the full set of filter kinds.
7. **What is `fields=`?** ✅ A projection — deliver only selected `CleanEmail` fields as a dict instead of the full object (`make_projection`). `"from"` aliases `from_`.
8. **What are `stages` / `clean_fn`?** ✅ Post-processing hooks run on each `CleanEmail` after extraction, before delivery (`StagesEmitter`). `clean_fn` runs first, then `stages`.
9. **What is the attachment policy?** ✅ `connect(attachments=...)` — a per-class strip policy (`AttachmentPolicy`/`AttachmentRule`) deciding which attachments to strip vs keep.
10. **What reasons can an attachment be stripped for?** ✅ `not_allowlisted`, `oversize`, `unreadable`, `scanner` (`StripReason`).
11. **Where do stripped attachments show up?** ✅ Recorded as `StrippedAttachment` on `CleanEmail.stripped_attachments` and counted in `RunReport.stripped`.
12. **What emitters are available?** ✅ memory, callback, queue, stdout, pubsub, stages-wrapper (`emit/` package).
13. **What is the `overrides=` seam?** ✅ Caller-supplied components (cursor/dedupe/blob store, cleaner, emitter, provider) that replace defaults — for testing or custom infra (spec §A10).
14. **What are `as_filter` / `as_cleaner`?** ✅ Decorators that tag a function's role for the `overrides=` seam; they return the function unchanged.
15. **How is state persisted?** ✅ `state="memory"` (default, stateless-ish) or `state="sqlite:///mf.db"` for restart-safety (`config/state.py`, `persistence/sqlite_store.py`).
16. **What does `health()` check?** ✅ Reachability of the cursor, dedupe, and blob stores backing the handle (`core/observability.health`).
17. **Is there a CLI?** ✅ Yes (in-flight) — `mailflow auth gmail` and `mailflow check gmail`. Entry point `mailflow = mailflow.cli:main`; also `python -m mailflow`.
18. **What does `mailflow auth gmail` do?** ✅ Runs browser OAuth consent, writes `GMAIL_REFRESH_TOKEN` to `.env` (chmod 0600), then does a live confirmation call.
19. **What does `mailflow check gmail` do?** ✅ Verifies Gmail read access (profile call per mailbox) and Pub/Sub consume access; prints pass/fail.
20. **Can I classify relevance with an LLM?** ✅ The `Classifier` port exists and runs on `uncertain` filter results. 🔶 Ask lead whether a real classifier is wired in production or still a no-op default.

---

## Section 4 — Cloud, Connection & Gmail Setup (20)

1. **Which cloud does the live path use today?** ✅ Google Cloud — Gmail API + Pub/Sub for push notifications.
2. **How does Gmail push work?** ✅ Gmail `watch` posts a notification to a Pub/Sub topic; mailflow consumes the subscription, then re-reads via the cursor (wake-signal only).
3. **Why "wake-signal only"?** ✅ Push payloads are never trusted as content; mailflow re-derives the actual messages from the cursor/history (`WebhookIdentity` carries identity, not payload).
4. **What credentials does Gmail need?** ✅ `client_id`, `client_secret_ref`, `oauth_refresh_token_ref`, mailbox(es), and Pub/Sub `project_id` / `topic` / `subscription`.
5. **How do I get a refresh token?** ✅ Run `mailflow auth gmail` — local-server OAuth with `access_type=offline` + `prompt=consent` guarantees a refresh token.
6. **What OAuth scope is requested?** ✅ `https://www.googleapis.com/auth/gmail.readonly` (read-only).
7. **Where are secrets read from?** ✅ A `SecretProvider` (default `EnvSecretProvider`) resolves refs from environment / `.env`.
8. **What's the `.env` security posture?** ✅ Written `0600` owner-only and git-ignored. 🔶 Ask lead — on Windows `chmod` is best-effort, so confirm the prod host is POSIX or uses a real secret manager.
9. **How are watches kept alive?** ✅ A renewal driver / scheduler re-arms the Gmail `watch` before it expires (`adapters/gmail/watch.py`, `scheduler.py`, `rotation.py`).
10. **What happens when the OAuth token rotates?** ✅ A `TokenRotationSink` persists the new refresh token so the next run survives (`adapters/gmail/rotation.py`).
11. **How does the webhook get verified?** ✅ `WebhookVerifier` proves the push is genuine and returns identity only (`adapters/gmail/webhook.py`).
12. **What's the setup checklist for Gmail?** ✅ See `docs/google-setup-step-by-step.md` + `docs/getting-started.md`. 🔶 Ask lead which GCP project / service account prod uses.
13. **What is `ordering_key` on the event?** ✅ The mailbox — lets a downstream ordered transport (e.g. Pub/Sub) preserve per-mailbox order.
14. **How does the batch/memory provider differ?** ✅ No cloud — seeded in-process emails, finite, drained in one `run_once`. Used for tests/demos.
15. **What does the Graph/Outlook path use?** ✅ Microsoft Graph + Azure Event Hubs (subscriptions, checkpoint blob store). Coded but not exposed via `connect()`.
16. **Can I run against multiple mailboxes?** ✅ Yes — `GMAIL_MAILBOXES` / `credentials["mailboxes"]` is a list; each is its own stream.
17. **Where does the blob (attachment) storage point in the cloud?** ✅ Via `BlobStore` port — local blob store by default; 🔶 Ask lead what prod uses (GCS? Azure Blob?).
18. **How does it recover after a restart?** ✅ With `state="sqlite:///..."` the cursor + dedupe survive; it resumes from the last committed cursor.
19. **What scripts help connect/verify?** ✅ `scripts/check_gmail_connection.py`, `get_gmail_refresh_token.py`, `run_gmail_live.py`, `run_gmail_to_inbox.py`.
20. **What deployment model is intended?** 🔶 Ask lead — library-embedded-in-an-app vs a long-running service. (Plan in notes: installable library first, SaaS later.)

---

## Section 5 — Security, Reliability & Operations (20)

1. **What is the DLQ (dead-letter queue)?** ✅ Two layers: a fire-and-forget `dlq_emitter` sink *and* a durable, queryable `DeadLetterStore` for redrive.
2. **Why a durable DLQ separate from the emitter?** ✅ So an operator can list, fix the root cause, and redrive — the emitter sink alone isn't queryable/deletable.
3. **What's the DLQ ordering guarantee on failure?** ✅ `dlq_store.put()` runs *before* the irreversible `mark_done`; if `put` raises, the message stays reclaimable and re-dead-letters later (idempotent by `record_id`).
4. **How is double-counting of DLQ avoided?** ✅ `dead_lettered` is counted only via `add_dead_letter()`, paired with the trace as a unit — exactly one count per poison message.
5. **What's the error taxonomy?** ✅ `PermanentError`→DLQ no retry; `AuthError`→refresh-and-retry-once; `TransientError`/unknown→bounded retry then DLQ (`core/errors.py`).
6. **How does auth-error recovery work?** ✅ One forced credential refresh + a single retry of the work body in-place (bounded by control flow, not the attempt counter), then DLQ.
7. **How is idempotency guaranteed end-to-end?** ✅ Claim-before-spend + monotonic cursor + `idempotency_key` on the emitted event; overlapping sweeps are tested to be idempotent.
8. **Is there a redrive tool?** ✅ Yes — `core/redrive.py` + `docs/dlq-redrive.md`.
9. **How are secrets kept out of logs?** ✅ Tokens flow through `SecretProvider` refs; the refresh token is written 0600. 🔶 Ask lead about central secret management in prod.
10. **What stops a huge email from OOM-ing the process?** ✅ The fail-closed size guard checks metadata before any download.
11. **How are attachments scanned?** ✅ `AttachmentScanner` port — default no-op allow-all. **Metadata only** (no bytes) in V1; a real AV/CDR scanner is a P2 evolution. 🔶 Ask lead if prod needs AV now.
12. **What's the test strategy?** ✅ TDD, 402 tests — idempotency, fail-closed guards, DLQ durability, token rotation, webhook idempotency, port conformance.
13. **How is type-safety enforced?** ✅ mypy `--strict` over the whole importable tree; every commit must leave the tree importable.
14. **What's the runtime port check for?** ✅ `_assert_port` structurally verifies injected components at construction — catches the `overrides=`/`connect()` seams mypy can't see.
15. **What observability exists?** ✅ `RunReport` counts + `DecisionTrace` per message + structured logging per disposition + `health()` probes.
16. **Where are QA findings tracked?** ✅ `docs/qa-findings.md` — read it before re-reporting; findings are classified by phase scope.
17. **Is there a single-writer guarantee on the cursor?** ✅ Yes — `commit_if_ahead` is monotonic CAS; rejects out-of-order commits.
18. **What's the dedupe TTL?** ✅ Done records kept ~60 days (`done_ttl_seconds`), matching the spec dedupe window.
19. **What happens if the blob store write fails during extract?** ✅ It surfaces as an error and flows through the retry/DLQ machinery like any other failure. 🔶 Ask lead about blob-store SLAs in prod.
20. **What's the biggest known limitation right now?** 🔶 Ask lead — candidates: Graph not exposed, classifier maybe no-op, attachment scanner metadata-only, Windows secret-perm gap.

---

## Section 6 — User / Product / Usage (20)

1. **Who is the user of this library?** ✅ App developers who want clean inbound email without touching MIME/OAuth. 🔶 Ask lead who the *first internal consumer* is.
2. **What's the simplest possible usage?** ✅ `for email in connect("memory", seed=seed).stream(): print(email.subject)`.
3. **What's the simplest real (Gmail) usage?** ✅ `mailflow auth gmail` once, then `connect("gmail", credentials=creds, mailbox="me")` and loop.
4. **What does the consuming app have to build itself?** ✅ Storage/DB, and any UI. mailflow stops at delivering `CleanEmail`.
5. **Is there a sample app?** ✅ `scripts/inbox_app.py` + `run_gmail_to_inbox.py` show end-to-end Gmail→inbox.
6. **How do I show this in a demo?** ✅ Run the memory provider for a deterministic offline demo, or `mailflow check gmail` to prove a live connection. 🔶 Ask lead which is safe to show.
7. **What fields will a product team care about on `CleanEmail`?** ✅ `from_`, `to`, `subject`, `body_text`, `attachments`, `thread_key`, `date_utc`, `labels`.
8. **How does threading work?** ✅ `thread_key` = Gmail threadId / Graph conversationId (subject-fallback in Phase 1).
9. **Can users get only the fields they need?** ✅ Yes — `fields=["from","subject","body_text"]` delivers a slim dict.
10. **Can users transform emails before delivery?** ✅ Yes — `clean_fn` / `stages` hooks.
11. **How does a user filter spam/unwanted mail?** ✅ `filters=[...]` with built-in kinds + custom functions; `uncertain` can defer to a classifier.
12. **What does the user see when something fails?** ✅ Nothing in the happy stream — failures go to the DLQ; ops inspect `RunReport`/DLQ. 🔶 Ask lead about user-facing error surfacing.
13. **Is multi-tenant supported for a SaaS?** ✅ The `tenant` field scopes state; 🔶 Ask lead if full SaaS isolation is on the roadmap (notes say "SaaS later").
14. **What's the onboarding doc for a new user?** ✅ `docs/getting-started.md`, `docs/mailflow-usage-guide.md`, `docs/mailflow-showcase.md`.
15. **How is the product status communicated?** ✅ `docs/mailflow-status-for-manager.md` + the architecture-review HTML decks under `docs/architecture-review/`.
16. **What's the naming/branding decision?** 🔶 Ask lead — notes say keep the name "mailflow"; confirm it's final for release.
17. **What's the first provider users get?** ✅ Gmail + memory. Outlook/Graph is later.
18. **What's the value proposition vs just calling Gmail API directly?** ✅ Normalization, dedupe, cursor/restart-safety, retries+DLQ, provider-agnostic output — all the hard reliability plumbing.
19. **Can a user swap storage backends?** ✅ Yes — `overrides=` and the store ports (memory/sqlite/local-blob today). 🔶 Ask lead which backends are blessed for prod.
20. **What's the roadmap a user should know?** 🔶 Ask lead — installable library → more providers (Outlook) → possible SaaS. Confirm timeline and priorities.

---

## How to use this when you present

1. **Lead with the one-liner** (top of this doc) + the architecture diagram idea: *facade → pipeline → ports → adapters*.
2. **Walk the 8-step pipeline** (Section 2) — that's the spine of the whole system.
3. **Demo** the memory provider (offline, safe) or `mailflow check gmail` (proves live).
4. **Show reliability** (Section 5): idempotency, DLQ, retries — this is what makes it production-grade, not a script.
5. **Take the 🔶 questions to your tech lead** — they're the genuine unknowns (deployment, prod backends, classifier status, roadmap).
