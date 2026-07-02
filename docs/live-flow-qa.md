# mailflow — Live Flow Deep-Dive Q&A (the "walk me through the mechanism" questions)

These are the follow-up-chain questions a tech lead asks when you say "emails come in and get processed."
Each answer names the **real function/file** so you can point at the code. Read top-to-bottom — it *is* the flow.

> **The 10-second version:** Gmail `watch` → Google publishes a tiny Pub/Sub notification (`{emailAddress, historyId}`) → our consumer pulls it → we ask Gmail "what changed since my last historyId?" (`history.list`) → for each changed message we download raw RFC822 (`messages.get format=raw`) → the pipeline parses/filters/extracts → emits a `CleanEmail` → acks the Pub/Sub message → advances the cursor.

---

## A. How emails arrive (the notification chain)

1. **Q: How does an email actually reach our system — do we poll Gmail?**
   A: Primarily **push, not poll**. We register a Gmail `watch` on the mailbox; Gmail then publishes a notification to a Google **Pub/Sub topic** every time the mailbox changes. We consume that subscription. (There's *also* a safety-net poll — see section F.) Code: `run_service` → `bootstrap_watches` → `run_consume_loop` in `adapters/gmail/live.py`.

2. **Q: What sets up the watch in the first place?**
   A: `bootstrap_watches()` (`adapters/gmail/bootstrap.py`) calls `watch_manager.ensure_watch(stream)` per mailbox. Without this, **zero notifications are ever published** — the watch is what links mailbox → topic.

3. **Q: What is a Gmail `watch` exactly and how long does it last?**
   A: A subscription registration that tells Gmail "push changes for this mailbox to this Pub/Sub topic." It **expires in ~7 days**, so we renew it (daily by default) via a background scheduler — `should_schedule_renew()` + `renew_watches()`.

4. **Q: When the watch is created, what do we store?**
   A: The watch returns a starting `historyId`. We **seed the cursor** with it: `cursor_store.commit_if_ahead(tenant, stream, Cursor(value=historyId, order=int(historyId)))`. That's the "I've seen everything up to here" bookmark, so the first diff has a start point.

---

## B. Pub/Sub — how it actually works here

5. **Q: How do we receive Pub/Sub messages — pull or push subscription?**
   A: **Streaming pull.** `run_consume_loop()` (`live.py`) calls `subscriber.subscribe(sub_path, callback=...)` and blocks on `future.result()`. Google's client library long-polls and invokes our callback per message.

6. **Q: What's the very first thing that happens when a notification lands?**
   A: The callback (`_make_message_callback`) hands the message to `runtime.process_messages([message])` (`adapters/gmail/runtime.py`).

7. **Q: First notification — what payload do you actually receive? Show me the bytes.**
   A: A tiny JSON body: **`{"emailAddress": "user@gmail.com", "historyId": 123456}`**. That's it — **no email content**. Depending on the delivery layer it may arrive plain or base64-encoded, so `parse_pubsub_message()` (`notifications.py`) tries plain JSON first, then base64-decodes and retries.

8. **Q: Why is there no email content in the notification?**
   A: By design — the `historyId` is just a **watermark / wake-signal**. We never trust the push payload as content; we re-derive the real messages from Gmail using the cursor. (Security rule A5: a webhook proves *who* changed, not *what*.)

9. **Q: So the historyId in the notification — do you use its value directly?**
   A: We use it as a "something changed, go look" signal and remember it as the *latest* pending watermark (`provider.submit(email_address, history_id)` stores it in `_pending[mailbox]`). But the **actual diff starts from the stored cursor**, not from the pushed value — that's what makes us not miss anything between notifications.

10. **Q: What does the second/next notification look like — same shape?**
    A: Same shape, higher `historyId`. Each one just bumps the watermark and triggers another `run_once()`. If 5 notifications arrive close together, each runs a diff; overlap is harmless because of dedupe (section E).

---

## C. Turning a notification into real emails (history diff + fetch)

11. **Q: You got `historyId`. How do you turn that into actual emails?**
    A: `GmailProvider.fetch()` (`provider.py`) calls `client.history_message_ids(mailbox, start, label_id)` — Gmail's **`history.list`** API — which returns the message IDs that changed since `start` (the stored cursor), plus the new latest historyId.

12. **Q: Where does `start` come from — the pushed historyId or the cursor?**
    A: **The stored cursor** (`cursor.value`). Only on the *very first* notification (no cursor yet) do we fall back to the submitted historyId, so we don't accidentally full-resync the whole mailbox.

13. **Q: `history.list` gives you IDs, not emails. How do you get the email itself?**
    A: For each message ID, `client.get_message_raw(mailbox, message_id)` calls Gmail **`messages.get` with `format=raw`**, which returns base64url RFC822. We decode it (`_b64url_decode`) into raw bytes and wrap it in a `RawMessage` carrying `raw_bytes`, `size_bytes` (`sizeEstimate`), and `thread_key` (Gmail `threadId`).

14. **Q: Why `format=raw` instead of Gmail's parsed JSON?**
    A: So Gmail needs **no provider-specific parser/extractor** — the raw RFC822 goes straight into our core `MimeExtractor`, the same one the memory provider uses. One extraction path for all providers.

15. **Q: What's the order of operations once you have a RawMessage?**
    A: The pipeline's per-message order (`core/pipeline.py`): **claim → size-guard → parse → filter → (classify) → extract → emit → mark-done.**

---

## D. Extraction — "how do you extract things?"

16. **Q: How does extraction actually work on the raw bytes?**
    A: `MimeExtractor.extract_bytes(raw, provider, provider_message_id, stream_id, watched_mailbox, blob_store, thread_key)` parses the RFC822 with Python's `email` library and builds a `CleanEmail`: headers → from/to/cc, subject, dates; body → text + html; attachments → metadata.

17. **Q: Where do attachment bytes go — into the email object?**
    A: **No.** Attachment bytes are streamed into the `BlobStore`; the `CleanEmail.attachments[]` carry a `storage_ref` pointer + `content_hash` (sha256), never the bytes. Keeps the emitted object small.

18. **Q: What's this "extractor seam" you mention?**
    A: `Pipeline._extract` dispatches by type: a `MimeExtractor` → `extract_bytes()` (raw RFC822, Gmail/memory); any other extractor → the generic `extract(msg, env)` port (so the Graph adapter can parse Graph JSON directly without touching the orchestrator).

19. **Q: What if an attachment is huge or not allowed?**
    A: The attachment policy (`connect(attachments=...)`) strips it: it's recorded as a `StrippedAttachment` with a `StripReason` (`oversize`/`not_allowlisted`/`unreadable`/`scanner`) and counted in `RunReport.stripped` — the email still flows, minus the attachment.

20. **Q: What's the final output handed to the app?**
    A: An `EmailEvent(schema_version, tenant, ordering_key=mailbox, idempotency_key, email=CleanEmail)` pushed to the configured `Emitter` (callback / queue / stdout / pubsub).

---

## E. "Lots of emails at once" — concurrency, duplicates, ordering

21. **Q: A burst of 500 emails arrives. How do you handle that without choking?**
    A: `history.list` returns **all changed IDs in one diff**, and `fetch()` yields them one at a time as a generator — we don't load 500 bodies into memory at once. V1 is deliberately **synchronous, one message at a time** (`SYNC_ONLY`), so memory stays bounded; throughput scales by running more instances, not threads.

22. **Q: Two notifications for the same mailbox arrive overlapping — won't you process emails twice?**
    A: No. **Claim-before-spend**: `dedupe_store.try_claim(idempotency_key, lease)` runs before any work. The second attempt on the same `(tenant, mailbox, provider_message_id)` fails the claim and is recorded as `duplicate`, not reprocessed.

23. **Q: What about the push path and the safety-net sweep hitting the same message?**
    A: Same protection — the dedupe claim + the **monotonic cursor** make overlap harmless. There's an explicit regression test: "overlapping sweep+push is idempotent and cursor-monotonic."

24. **Q: How do you guarantee you don't lose ordering for a mailbox?**
    A: The emitted event's `ordering_key` is the mailbox, so an ordered downstream (e.g. Pub/Sub with ordering) preserves per-mailbox order. The cursor is **single-writer, monotonic** (`commit_if_ahead` rejects any commit not strictly ahead).

25. **Q: If processing message #3 of 10 fails, do messages #4–10 get blocked?**
    A: Depends on the failure. A **poison single message** (404/410/invalid base64) is skipped so the rest of the batch flows (`PermanentError` → `continue` in `fetch`). A **whole-stream problem** (auth/transient) propagates so the cursor does *not* advance past unread mail — we'd rather retry than lose mail.

26. **Q: When is a Pub/Sub message acked — before or after processing?**
    A: **After.** "The ack IS the checkpoint" (`runtime.py`): `message.ack()` runs only after `pipeline.run_once()` returns successfully. A failed run is **not acked**, so Pub/Sub redelivers it. (An unparseable notification is acked because there's nothing to process.)

27. **Q: What stops a giant email from OOM-ing you during a burst?**
    A: The **fail-closed size guard** checks reported size *before* downloading bytes (default 50 MB cap). Unknown/zero size is treated as over-limit and dead-lettered — we never download something we can't vouch is within budget.

---

## F. Reliability — what if the push misses something?

28. **Q: Pub/Sub isn't 100% reliable. What if a notification never arrives?**
    A: A **safety-net sweep** runs on a background timer (`sweep_once`, `gmail_cfg.sweep_seconds`). Per mailbox it submits the current `historyId` and runs the pipeline; `fetch` diffs from the **stored cursor**, catching anything the push missed. Idempotent, so it never double-delivers.

29. **Q: What if the stored historyId is too old (Gmail forgot that far back)?**
    A: `history.list` raises `StaleHistoryError`; `fetch` catches it and **re-seeds** the cursor to the mailbox's current `historyId` (`_reseed`). The gap mail is skipped — the safe recovery so the mailbox isn't stuck forever.

30. **Q: What happens to a message that keeps failing?**
    A: Bounded retries (`max_attempts`, default 3), then it's **dead-lettered**: a durable `DeadLetterStore.put()` record (replayable) + the cursor advances past it so the stream isn't blocked. `put()` runs *before* the irreversible `mark_done`, so a store failure leaves it reclaimable.

---

## G. Auth & the refresh token — "how do you get and keep it?"

31. **Q: How do you authenticate to Gmail — service account or user OAuth?**
    A: **Per-user OAuth refresh token** (works with a personal `@gmail.com` too). The operator supplies client secret + refresh token via the `SecretProvider`; nothing is hardcoded (`live.py` header).

32. **Q: How do you actually *get* the refresh token the first time?**
    A: Run **`mailflow auth gmail`** (`cli.py` → `auth.py:get_gmail_refresh_token`). It runs Google's local-server OAuth consent flow in the browser with **`access_type=offline` + `prompt=consent`** — those two flags guarantee Google returns a refresh token. Scope: `gmail.readonly`.

33. **Q: Where does the refresh token get stored?**
    A: Written to `.env` as `GMAIL_REFRESH_TOKEN` via `upsert_env_var()`, clamped to `0600` owner-only and git-ignored. (On Windows `chmod` is best-effort — a prod secret manager is the real answer.)

34. **Q: Refresh token → access token. When/how does that happen at runtime?**
    A: `OAuthTokenProvider.get_token()` (`live.py`): if the cached access token isn't valid, it calls `creds.refresh()` to mint a new one from the refresh token. Access tokens are short-lived; the refresh token is the durable credential.

35. **Q: Google sometimes rotates the refresh token. Do you lose access on restart?**
    A: No. `_refresh_and_maybe_rotate()` detects when Google hands back a **new** refresh token and persists it via the `TokenRotationSink` (`on_refresh(ref, new_token)`), so the next process start survives (rule A8).

36. **Q: What happens on a 401 mid-run?**
    A: `force_refresh()` unconditionally re-mints the access token (ignoring the locally-cached `.valid`), then the pipeline retries the work body **exactly once**; a second failure dead-letters (`AuthError` path in `pipeline.py`).

37. **Q: Do you check the token actually has the right permissions before running?**
    A: Yes — `verify_oauth_scopes()` runs at startup (before any watch is registered) and raises `AuthError` naming any missing scope, so a mis-scoped credential fails fast instead of silently under-delivering. (Fail-open caveat on very old `google-auth` that doesn't report `granted_scopes`.)

---

## H. Likely curveballs your lead throws

38. **Q: Why Pub/Sub at all — why not just poll every minute?**
    A: Push is near-real-time and cheap (Gmail only pings on change); polling wastes quota and adds latency. We keep a *slow* sweep as a safety net, not the primary path.

39. **Q: Can this run for multiple mailboxes?**
    A: Yes — `mailboxes` is a list; each is its own `StreamRef` with its own cursor and watch. `_pending` is keyed by mailbox.

40. **Q: What's the single most important reliability property here?**
    A: **Idempotency + monotonic cursor.** Push, sweep, redelivery, and restarts can all re-present the same message; claim-before-spend + a strictly-ahead cursor guarantee each email is emitted once and the bookmark only moves forward.
