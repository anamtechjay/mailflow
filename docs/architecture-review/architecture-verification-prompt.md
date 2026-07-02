# Architecture Verification — Provider-Agnostic Email-Processing Library

## Goal
We have a working email-processing implementation (currently Microsoft Graph / Outlook).
Before building further, INDEPENDENTLY VERIFY that the core architecture is solid, complete,
and genuinely provider-agnostic. The product vision: let developers process inbound emails
(Gmail, Outlook, others) with minimal boilerplate, overriding any stage via injected functions.

This is a VERIFICATION pass, not a feature wishlist. For EACH concern below, answer:
  (a) Is there a port/seam for it, or is it hardcoded?
  (b) Is the contract correct, and is it safe to FREEZE (changing it post-1.0 breaks consumers)?
  (c) Does it carry BOTH Gmail and Graph without contract changes? (prove, don't assume)
  (d) Is it must-have-now or a deferred extension point whose SHAPE must still land now?

## Cross-cutting invariants the architecture MUST honor (verify each)
- **Notifications are wake-signals only.** The pipeline NEVER fetches by a message-id /
  historyId / mailbox taken from a push payload; it always re-derives work from the durable
  cursor via delta (Graph) / history.list (Gmail). This is the keystone rule.
- **At-least-once everywhere.** Drop all "exactly-once" framing. Every consumer-facing output
  carries idempotency_key = (tenant, mailbox, provider_message_id); document "consumers MUST
  dedupe on this key." Order side effects: write blobs → emit → mark_done, so a crash yields
  only re-derivable duplicates.
- **Cursor contract (freeze now):** Cursor.value is a DURABLE resume token
  (Graph = deltaLink; Gmail = historyId — NEVER an ephemeral pageToken). Cursor.order is a
  monotonic CAS anchor that keeps climbing across resync. The cursor advances ONLY to the max
  FULLY-PROCESSED unit — never to a notification/watch-response historyId (out-of-order Pub/Sub
  would silently skip mail).
- **One typed error taxonomy:** AuthError / PermanentError / TransientError (subclass a base),
  raiseable at transport AND extract layers AND by INJECTED components. Orchestrator routes:
  Permanent (404/410 at extract, MIME/charset/base64 malformation, oversized) → DLQ, no retry;
  Transient (429/5xx) → release + retry with attempt cap + Retry-After backoff;
  Auth (401) → invalidate + refresh + retry-once; 403 → Permanent (scope/policy), no retry.
- **Provider-agnostic, proven.** Keep all Graph-specific choices OUT of the core (per-folder
  StreamRef, $top=1 page trick, deltaLink-as-cursor are GRAPH choices, not core assumptions).

## 1. Listening / subscription lifecycle
- Pluggable consumption: webhook push AND poll/delta — with poll/delta as the AUTHORITATIVE
  ingestion engine and push layered on top as a wake-signal optimization.
- **Renewal driver (not just detection):** a scheduler that actively CALLS renew. Graph: renew
  ~24h against the ceiling (NOTE: fix MAX expiry to ≤4230 min for Outlook MESSAGE resources —
  currently requests ~10_020 and Graph 400s every subscribe). Gmail: re-call users.watch()
  DAILY (its only renewal AND dropped-watch path — Gmail emits NO lifecycle events).
- **Dropped-subscription detection:** Graph lifecycle events (reauthorizationRequired /
  subscriptionRemoved / missed) vs Gmail's client-side timer + reconciliation poll.
- **Missed-email backfill / recovery** (the silent-data-loss risk): Graph delta resume;
  Gmail historyId resume, with a BOUNDED fallback when historyId expired (~7d retention) →
  messages.list q=after:<watermark> so a reset never re-enumerates the whole mailbox.
- Subscribe / unsubscribe / teardown; durable, reconciled subscription state (GET /subscriptions)
  to avoid orphan/duplicate subscriptions.
- **StreamRef granularity is provider-defined:** Gmail = exactly ONE mailbox-level stream
  (one historyId); do NOT map Gmail labels to per-folder streams. Filter own SENT/DRAFT/SPAM.

## 2. Ingestion & content model
- Normalized cross-provider CleanEmail (body, metadata, headers, attachments, labels/flags).
- **Body parity:** body_text must be populated IDENTICALLY across providers (Graph returns HTML
  → add uniform HTML→text fallback in both extractors). This is the cheapest proof of the
  provider-agnostic body contract.
- MIME robustness: multipart/nested, encodings/charsets, inline cid: parts.
- **Threading:** add a provider-native thread key (Gmail threadId / Graph conversationId) +
  subject-normalized fallback (RFC822 In-Reply-To/References are unreliable on Outlook).
- Thin auto_submitted / is_bounce boolean seam now; full bounce/OOO/DSN classification deferred.

## 3. Filtering
- Built-in blacklist/whitelist by To/CC/subject (incl. regex) AND injectable custom filter fns
  (payload → bool). Function-adapter sugar (as_filter decorator) so consumers needn't wire ports.

## 4. Attachments
- Define payload shape: full bytes vs marker/reference + follow-up fetch.
- **Stream large attachments** (no full buffering) with a byte cap that aborts mid-stream
  (fail-closed); size limits.
- **Safety seam BEFORE permanent storage:** content-type/extension allowlist + scan/quarantine
  hook (no-op default; ClamAV/Defender deferred).
- **Duplicate dedup by content_hash** (the hash is already computed — wire it).
- Expose cid → storage_ref MAPPING on CleanEmail; do NOT rewrite body_html in the core.

## 5. Cleaning
- ContentCleaner port, per-stage isolated (a cleaner failure DLQs the message, not the run).
- **Thin default only:** HTML→text fallback. Quoted-reply + RFC3676 "-- " signature stripping
  are OPT-IN bundled cleaners — never default (the library must not silently mutate bodies).

## 6. Storage
- BlobStore port; one reference impl (local). Multiple backends (S3 + format choice) deferred.
- **Content-addressed writes** (by content_hash) so sinks are naturally idempotent.
- Land delete()/erase() signatures + an encryption-context arg on put_stream NOW (even as stubs)
  — these are consumer-implemented Protocols; adding methods later breaks implementors.

## 7. Secrets, auth & token lifecycle
- Widen SecretProvider beyond get(ref)→str: per-tenant resolution; a per-mailbox authorized-
  transport seam (Gmail domain-wide-delegation subject impersonation); and a refresh-token
  ROTATION/revocation PERSISTENCE callback (a rotated token that can't be written back bricks
  the next run). Wire 401→refresh→retry-once / 403→permanent through the transport.
- Document the auth models: app-only (Graph Mail.Read is org-wide; needs an Exchange Application
  Access Policy to scope mailboxes) vs delegated vs Gmail DWD.

## 8. Deduplication & identity
- Consumer supplies a DB URL; library does all bookkeeping. Keep the DedupeStore shape
  (try_claim/record_attempt/mark_done/release) so a persistent adapter can add lease expiry.
- Make max-attempts LIFETIME-counted in the persistent store; quarantine flapping poison after
  N total attempts. done_ttl MUST stay > provider resync window (Gmail ~7d).

## 9. Delivery semantics & reliability
- **WebhookVerifier port** returning a VERIFIED subscription identity only (never mailbox/message
  from payload): Graph = constant-time clientState + validationToken; Gmail = OIDC-JWT (verify
  sig, iss, aud, exp, push-SA email). Fail-closed: reject the whole batch on any forged item
  (safe because notifications are wake-signals).
- Ack timing; ordering (strict in-order HALT is the v1 DEFAULT but an explicit, documented
  POLICY); retries + Retry-After-aware bounded backoff + concurrency cap + circuit breaker.
- **Replayable DLQ:** a refetchable handle (provider + message_id + failure metadata) or raw
  bytes + a documented redrive path (replace any non-replayable stub).
- Rate limits: Graph throttling vs Gmail quota-unit 429/403; throttle the resync/backfill path
  and checkpoint within it.

## 10. Processing model
- **Declare sync-only this phase** (ports return sync Iterator); design the async seam so it's
  additive later. Per-message DLQ isolation: one malformed page/delta record DLQs INDIVIDUALLY
  and paging continues. Failure-notification hook for consumers.

## 11. Observability & compliance
- Logging, metrics, tracing, run reports, health checks.
- Bound/redact PII in traces, DLQ records, and the default emitter (no full-body stdout).
- Least-privilege scopes; make named-but-dead security config (startup scope verification /
  read allowlist) REAL or delete it (don't leave false assurances).

## 12. DX & API stability
- builder overrides= for direct component/callable injection; as_filter / as_cleaner sugar.
- Fail-fast config validation at validate() time (e.g. Graph requires "mailbox") + port-
  conformance smoke check.
- Publish a port-stability / config-version policy (provisional vs stable) so added fields and
  the future async port family are non-breaking. Defer the open string-kind register_kind()
  plugin path (needs a trust/allowlist model).

## 13. Confirm-or-fix (defects the panel flagged in the current code)
- Graph subscription expiry 10_080 → ≤4230 min (live blocker).
- Size guard fails OPEN on unknown size (Graph omits size) → route unknown size to
  DLQ-without-download or a Content-Length probe.
- Per-message exception escapes the run loop → wrap per-message materialization.
- 404/410 and invalid-base64 currently treated as transient → reclassify via taxonomy.
- Replace $top=1 page==message trick with PAGE-ATOMIC cursor commit (advance only at page
  boundary; dedupe absorbs intra-page replay) — same no-loss guarantee without 1-HTTP-per-msg.

## Product decisions the review must resolve (cannot be decided silently)
1. **Scope:** does this phase BUILD a second provider (Gmail) end-to-end, or is a paper/thin
   conformance audit of the ports against Gmail's model sufficient? (Architecture is unproven
   provider-agnostic with only Graph.)
2. **Push vs poll for v1:** poll/delta as authoritative + webhook as wake-signal seam (durable
   inbound buffer & 202-fast-ack DEFERRED), or is real-time webhook ingestion a phase-1 req?
3. **Auth model & tenancy:** app-only vs delegated vs Gmail DWD; one credential set vs
   per-tenant/per-mailbox least-privilege — this fixes the SecretProvider shape before freeze.
4. **Multi-tenant now or single-tenant POC?**
5. **Gmail scope (if Gmail is in):** gmail.readonly (full body/attachments, triggers CASA audit)
   vs gmail.metadata (no body/raw — would gut cleaning & attachments).
6. **Concurrency horizon:** multiple workers per stream soon? If yes, fencing tokens + lease
   heartbeat become must-have now; if not, the single-in-flight invariant suffices.
7. **Ordering policy:** strict in-order (poison blocks its stream until DLQ) vs availability-first.
8. **GDPR/retention:** must deletion/erasure work now, or is a frozen stub acceptable?
9. **Does the SDK ever hand body_html to a renderer/UI?** Decides whether the default cleaner
   must sanitize HTML or whether HTML→text is an acceptable secure default.

## Scope guidance
MUST-HAVE ("solid & complete"): every freeze-sensitive port/contract above + the listed silent-
correctness defects + the two live blockers + a second-provider conformance proof.
DEFERRED (design the seam now, implement later): rich cleaning heuristics, malware scanner, KMS
encryption-at-rest, full GDPR DSAR tooling, transactional outbox / fencing / lease heartbeat,
register_kind() plugin path, multiple storage backends, durable async push buffer + fast-ack,
cid: body_html rewrite.
