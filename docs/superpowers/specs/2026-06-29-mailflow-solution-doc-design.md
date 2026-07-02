# mailflow — Solution Doc (HTML) · Design Spec

**Date:** 2026-06-29 · **Author:** Anamul Hasan
**Goal:** A single, self-contained HTML solution document to present mailflow to a technical lead
in a meeting. Primary outcome: **understand + align** (architecture walkthrough + design-review
conversation), not formal sign-off.

## Sources of truth
- `docs/architecture-review/features.md` — prioritized roadmap (P0–P3), glossary, Gmail↔Outlook diffs.
- `docs/architecture-review/v1-build-plan.md` — phased construction plan + ownership tracks.
- `docs/architecture-review/v1-readiness-report.md` — graded item-by-item readiness (status, %, effort).

## Scope of the doc
- **V1-focused with roadmap context.** Lead with the locked V1 scope (Gmail-only, single-tenant,
  poll-authoritative with Gmail push as a wake-trigger). Show P1/P2/P3 as the "where it goes next" map.
- Audience is **technical** — keep technical depth (contracts, signatures, error routing, data flow);
  use the plain-English "mailroom" metaphor only as an entry hook, not as the substance.

## Output
- Single file: `docs/architecture-review/solution-doc.html`.
- Diagrams via **Mermaid, vendored locally** (`docs/architecture-review/mermaid.min.js`) so they
  render **offline** in a meeting room. The two files travel together.
- No build step, no framework. Plain HTML + CSS + Mermaid. Opens with a double-click.

## Structure (three altitudes, top to bottom)
1. **The 60-second frame** — one-sentence definition + mailroom hook + the end-to-end journey diagram.
2. **Architecture at a glance** — the core idea: a library of **frozen contracts (ports)** with
   swappable implementations; build philosophy "freeze the contracts, then build in parallel."
   Diagram: core ↔ ports ↔ adapters.
3. **The pipeline, stage by stage** — the heart. One collapsible section per stage:
   - Wake signal & webhook verification (A5 — Gmail OIDC-JWT only in V1)
   - Fetch & cursor (A1/A3/B6 — wake-signals-only rule, durable monotonic cursor)
   - Size guard (B1 — fail closed on unknown size)
   - Extract fields + thread key (A7)
   - Clean HTML→text (A6/B5 — thin default cleaner, cross-provider body_text parity)
   - Filter (A10 — injected filters)
   - Emit + idempotency (A4 — idempotency_key on the wire, at-least-once)
4. **Cross-cutting concerns** —
   - Error taxonomy & routing (A2/B3 — Auth/Permanent/Transient → refresh/DLQ/backoff)
   - Reliability (B2 DLQ isolation, B4 backoff + storm containment)
   - Extensibility (A8 rotation persistence, A10 overrides/decorators, A11 sync-only + versioning)
   - Observability (light: logs/metrics/run reports)
5. **Scope & roadmap** — V1 locked scope; P1/P2/P3 map; deferred calls (Outlook live, GDPR, PII redaction).
6. **Where it stands + path to done** — completeness snapshot, done-vs-missing, the 3-phase build plan.

## Per-section card layout (repeatable, scannable)
Each pipeline/concern section uses one consistent card:
> **Stage name** · *plain-English one-liner* · **Contract:** `frozen signature/field` ·
> **Why it matters** (the failure it prevents) · **Status chip** (✅/🟡/❌ from readiness report) ·
> *optional diagram*

This keeps every section self-contained so the user can jump to and explain any one independently.

## UX
- Sticky side nav listing the sections — click to jump live during the meeting.
- Status color chips (done / partial / missing) drawn from the readiness grades.
- Light, clean, technical theme. No animation gimmicks.

## Diagrams to reuse/adapt (from the source docs)
- End-to-end journey (mailroom flowchart) — from features.md / readiness report.
- Error-taxonomy routing flowchart — from readiness report A2.
- Build harness phases flowchart — from build plan.
- Completeness-by-category bar + top-gaps quadrant — from readiness report (optional, status section).
- New: core↔ports↔adapters architecture diagram (authored for this doc).

## Non-goals (YAGNI)
- No interactivity beyond nav + collapsible sections.
- No live data / no JS framework.
- Not a status dashboard — readiness is one compact section, since the meeting goal is align, not report.
