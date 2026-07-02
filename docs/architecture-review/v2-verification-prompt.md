# V2 Readiness Verification

## Goal
Verify that the mailflow library has everything required for V2 — AND, critically, that V2 features
build cleanly on the seams frozen in V1 WITHOUT breaking any V1 contract. V2 = the P1 should-haves
that weren't shipped in V1 plus the P2 "seam-now, implement-later" set in `features.md`. This is an
audit of the ACTUAL codebase. Run the V1 verification FIRST; V2 readiness assumes V1 is GO.

## Method (follow exactly)
For each checklist item below:
1. Locate the relevant code (the V1 seam it plugs into, the V2 impl, tests). Cite `file:line`.
2. Classify it:
   - ✅ **Present & correct** — implemented and behaves as specified.
   - ⚠️ **Seam present, impl missing/partial** — the V1 contract/hook exists but the V2 behavior
     isn't built yet (this is the EXPECTED state for most P2 items — that's fine, flag it).
   - ❌ **Seam missing** — V2 can't be built without a breaking change to a frozen V1 contract.
     This is the serious finding: it means V1 froze the wrong shape.
3. For every ❌, state exactly which V1 contract would have to change and why that's breaking.
4. Report only; do not implement.

## Output format
- A table: `Item | Status | V1 seam it depends on (file:line) | Breaking-change risk`.
- A **V2 FEASIBILITY** verdict: can all V2 features be added as NON-breaking, additive changes?
  List any item that would force a breaking change (the highest-priority finding).
- A suggested V2 build order based on dependencies.

## Checklist

### A. P1 carryover (ship early in V2 if not already in V1)
- [ ] Subscription renewal DRIVER (scheduler actively calls renew; Graph ~24h, Gmail daily watch).
- [ ] Dropped-subscription detection + reconciliation (lifecycle events / timer + poll).
- [ ] Missed-email backfill / recovery, bounded by a receivedDateTime watermark.
- [ ] Replayable DLQ + documented redrive path; lifetime-counted attempt cap.
- [ ] Attachment streaming + fail-closed byte cap; safety seam (allowlist + scan hook).
- [ ] Attachment dedup by content_hash wired through.
- [ ] Observability: metrics, tracing, run reports, health checks; PII redaction.

### B. P2 — built on V1 seams (verify the seam exists & is the right shape)
- [ ] **Rich cleaning heuristics** (quoted-reply, RFC3676 signature) as opt-in cleaners
      → plugs into the V1 `ContentCleaner` port.
- [ ] **HTML sanitizer** (if body_html reaches a renderer) → ContentCleaner port.
- [ ] **Malware scanner** (ClamAV/Defender) → V1 attachment safety hook.
- [ ] **KMS encryption-at-rest** → V1 encryption-context arg on BlobStore.
- [ ] **Multiple storage backends** (S3 + format choice) → V1 `BlobStore` port.
- [ ] **Persistent DedupeStore adapter** (Firestore/Redis + lease expiry) → V1 DedupeStore shape
      (try_claim/record_attempt/mark_done/release) accommodates expiry without caller changes.
- [ ] **Durable async inbound buffer + 202 fast-ack** → V1 WebhookVerifier + wake-signal seam;
      poll/delta remains the engine.
- [ ] **Transactional outbox / fencing tokens / lease heartbeat** → requires the single-in-flight
      invariant documented in V1; verify concurrency model didn't bake in assumptions that block it.
- [ ] **cid → body_html rewrite** → V1 exposed the cid→storage_ref mapping (not a body rewrite).
- [ ] **GDPR DSAR tooling / retention sweeper** → V1 delete()/erase() store stubs.
- [ ] **Bounce/OOO/DSN (RFC3464) classification** → V1 thin `auto_submitted`/`is_bounce` seam.

### C. Async port family (if V2 introduces async)
- [ ] Async ports are ADDITIVE to the sync Protocols frozen in V1 (no retype of existing ports).
- [ ] Config-version policy from V1 lets new fields/ports land as minor, non-breaking bumps.

## The one question this pass must answer
**Did V1 freeze the right seams?** If every V2 item above is ✅ or ⚠️ (seam present, impl pending),
V1's architecture is validated. Any ❌ (seam missing → breaking change required) is a defect in the
V1 freeze that should be corrected before V1 ships, not in V2.
