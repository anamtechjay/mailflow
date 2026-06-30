# V1 Readiness Verification

## Goal
Verify that the mailflow library has EVERYTHING required for a solid, complete V1 — and nothing
more is being treated as a blocker than needs to be. V1 = the P0 tier in `features.md`: the
freeze-now contracts, the silent-correctness defects, the live blockers, and the second-provider
proof. This is an audit of the ACTUAL codebase, not a design discussion.

## Method (follow exactly)
For each checklist item below:
1. Locate the relevant code (port/Protocol, impl, test). Cite `file:line`.
2. Classify it:
   - ✅ **Present & correct** — exists, contract is right, behaves as specified.
   - ⚠️ **Present but wrong/incomplete** — exists but contract is off, or impl has a defect, or
     it's Graph-shaped in a way that won't carry Gmail.
   - ❌ **Missing** — no code for it.
   - 🔲 **Contract-only OK** — for items whose V1 requirement is only the frozen contract: confirm
     the signature/shape is frozen correctly; a thin/stub impl behind it is acceptable.
3. Give one line of evidence and, for ⚠️/❌, the minimal fix.
4. Do NOT fix anything in this pass — report only.

## Output format
- A table: `Item | Status | Evidence (file:line) | Fix if not ✅`.
- A **V1 GO / NO-GO** verdict with the count of ❌ + ⚠️ blockers remaining.
- A short list of anything currently treated as a V1 blocker that should actually be deferred.

## Checklist — must all pass for V1

### A. Freeze-now contracts (contract correct = mandatory; thin impl OK where noted)
- [ ] **Notifications are wake-signals only** — pipeline never fetches by a push-supplied
      message-id/historyId/mailbox; always re-derives from the durable cursor.
- [ ] **Typed error taxonomy** — Auth/Permanent/Transient subclasses, raiseable by injected
      components, routed by the orchestrator (Permanent→DLQ, Transient→retry, Auth→refresh-once).
- [ ] **Cursor contract** — value = durable token (Gmail historyId / Graph deltaLink, never a
      pageToken); order monotonic; advances only to max FULLY-processed unit.
- [ ] **idempotency_key on EmailEvent** — `(tenant, mailbox, provider_message_id)`; at-least-once
      documented; no exactly-once framing.
- [ ] **WebhookVerifier port** — returns verified subscription identity only (never payload
      mailbox/message); Graph clientState+validationToken / Gmail OIDC-JWT shape present.
- [ ] **ContentCleaner port** — per-stage isolated; thin HTML→text default (no aggressive
      stripping by default).
- [ ] **Provider-native thread key on CleanEmail** — threadId/conversationId + subject fallback.
- [ ] **Widened SecretProvider** — per-tenant resolution, per-mailbox transport seam, refresh-
      token rotation/persistence callback.
- [ ] **Store erasure + encryption-context** — delete()/erase() signatures + encryption-context
      arg on BlobStore.put_stream (stub impl OK).
- [ ] **Builder `overrides=` + `as_filter`/`as_cleaner` sugar** — injection without hand-wiring.
- [ ] **Sync-only declaration + port-stability/config-version policy** — async path is additive.
- [ ] **Provider-defined StreamRef granularity** — Gmail one-stream; Graph per-folder choice not
      baked into core.

### B. Silent-correctness / availability defects (impl must be fixed)
- [ ] Size guard fails CLOSED on unknown size (Graph omits size).
- [ ] Per-message DLQ isolation — one malformed page record doesn't abort the run.
- [ ] 404/410 & invalid-base64 classified Permanent (not retried).
- [ ] Retry-After-aware bounded backoff + concurrency cap at the transport layer.
- [ ] Cross-provider body_text parity (HTML→text fallback in both extractors).
- [ ] Page-atomic cursor commit (no `$top=1` 1-HTTP-per-message trick).

### C. Live blockers (impl must be fixed)
- [ ] Graph subscription expiry `10_080 → ≤4230` min.
- [ ] Graph size-probe extended-property verified on a live delta call (or replaced with cap).

### D. Verification gate
- [ ] Second-provider conformance proof — at minimum a paper audit of every port against Gmail's
      model (cursor=historyId, one mailbox-level stream, OIDC-JWT); thin Gmail adapter preferred.

## Out of scope for this pass
Anything in P1/P2/P3 of `features.md`. If a P1+ item is found MISSING, note it as "expected —
deferred," not a V1 gap. Judge only against the P0 bar above.
