# QA Automation (pytest) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Automated pytest coverage of every mailflow functionality and its edge cases — pipeline, content path, I/O, the four operational scenarios (bulk / duplicate / restart / large-MB), and property invariants.

**Architecture:** A thin shared harness under `tests/` (email builder + corpus generator + fake transports + a pipeline builder), then one test file per functionality. **pytest** for everything; **hypothesis** for "all orderings" invariants. No browser, no Playwright — mailflow is a headless library. All tests run with zero network via fakes.

**Tech Stack:** Python 3.13, pytest, `hypothesis`. Tests + fixtures only — **no `src/` changes.**

## Global Constraints

- **No production code edits.** If a test finds a real bug, log it in `docs/qa-findings.md`; don't patch `src/`.
- **Zero network** everywhere except the `@pytest.mark.live` gated suite. Fakes only.
- **mypy --strict clean; pristine output** (warnings = failures).
- **Every test asserts real behavior** — no `assert True`, no vacuous tests.
- **Naming:** test functions use the exact names in the Coverage Map (Part A) so the map and the suite stay 1:1.
- **Git:** local-only — do **not** push. "Commit" steps are optional local checkpoints.

## File Structure

```
tests/
  conftest.py                       # fixtures: mem_stores, sqlite_stores, sink
  _harness/
    __init__.py
    email_builder.py                # Email spec -> .as_rfc822()/.as_graph_json()
    corpus.py                       # bulk_seed(n), large_attachment_raw(nbytes), TRICKY
    fakes.py                        # build_memory_pipeline(...), FaultExtractor, Graph fake
  corpus/*.eml                      # golden tricky-mail set (Phase 2)
  qa/
    test_f01_dedupe.py … test_f17_fetch.py
    test_scenarios_{bulk,dup,restart,large}.py
    test_property_invariants.py
```

---

# PART A — Coverage Map (edge cases → test cases)

**This is the contract.** Each functionality lists every edge case, the test function that proves it, and the key assertion. Part B implements these in phases. `F18` (observability) is already covered by 57 passing tests and is the reference model.

### F01 — Dedupe / claim  *(seam + property)*
| Edge case | Test function | Key assertion |
|---|---|---|
| same message delivered twice | `test_same_message_twice_emits_once` | 1 event; `report.duplicates == 1` |
| same message, two mailboxes | `test_same_msg_two_mailboxes_emits_twice` | 2 events (key includes mailbox) |
| claim is exclusive | `test_claim_is_exclusive_sequential` | 2nd `try_claim` → False |
| mark_done suppresses redelivery | `test_mark_done_suppresses_redelivery` | no re-emit after done |
| release resets attempt count | `test_release_resets_attempt_count` | attempts back to 0 |

### F02 — Size guard  *(unit)*
| Edge case | Test | Assertion |
|---|---|---|
| oversized → DLQ, no download | `test_oversized_dead_letters_without_extracting` | extractor spy never called; `dead_lettered == 1` |
| exactly at the limit | `test_exactly_at_limit_emits` | `emitted == 1` |
| one byte over | `test_one_byte_over_dead_letters` | `dead_lettered == 1` |
| zero-byte message | `test_zero_byte_message` | handled, not crash |

### F03 — Envelope parse  *(golden corpus)*
| Edge case | Test | Assertion |
|---|---|---|
| missing `From` | `test_missing_from_fallback` | fallback address, no crash |
| absent Message-ID | `test_absent_message_id_parses` | Envelope built |
| folded header | `test_folded_header` | value reassembled |
| encoded-word subject | `test_encoded_word_subject` | decoded subject |
| garbage/absent Date | `test_bad_date_is_none` | `date_utc is None` |
| multiple `From` | `test_multiple_from` | first/spec'd one wins |

### F04 — Identity (canonical_id / key)  *(unit + property)*
| Edge case | Test | Assertion |
|---|---|---|
| trusted `<…@…>` Message-ID | `test_trusted_message_id_used` | used verbatim |
| untrusted (no `@`, spaces) | `test_untrusted_falls_back_to_hash` | `stable_hash` path |
| absent Message-ID | `test_absent_message_id_hash` | `stable_hash` path |
| same id, different mailbox | `test_same_id_diff_mailbox_diff_key` | different `idempotency_key`, same `canonical_id` |
| determinism | `test_stable_hash_deterministic` | same input → same id |

### F05 — Filtering + on_filtered  *(unit + integration)*
| Edge case | Test | Assertion |
|---|---|---|
| drop suppresses + reports | `test_drop_reports_dropped_and_suppresses` | not delivered; trace `dropped`, `matched_filter`, `reason` |
| tag delivers marked email | `test_tag_delivers_marked_email` | delivered; `disposition="filtered"` on CleanEmail |
| empty chain keeps | `test_empty_chain_keeps` | emitted |
| chain short-circuits | `test_chain_short_circuits` | first decisive wins |
| function filter raises | `test_function_filter_raise_contained` | contained, message handled |
| blacklist case-insensitive | `test_blacklist_case_insensitive` | matches regardless of case |

### F06 — Classifier  *(unit)*
| Edge case | Test | Assertion |
|---|---|---|
| invoked only on abstain | `test_classifier_only_on_abstain` | not called on decisive keep/drop |
| classifier raises | `test_classifier_raise_contained` | contained |
| score recorded | `test_classifier_score_recorded` | verdict on CleanEmail |

### F07 — Extraction → CleanEmail  *(golden + parity)*
| Edge case | Test | Assertion |
|---|---|---|
| **gmail == graph parity** | `test_gmail_graph_parity` | identical normalized CleanEmail |
| HTML-only | `test_html_only_to_text` | `body_text` derived |
| multipart/alternative | `test_multipart_alternative` | text part chosen |
| malformed base64 attachment | `test_malformed_base64_permanent` | `PermanentError` → DLQ |
| missing body | `test_missing_body_empty` | empty body, no crash |

### F08 — Attachments & caps  *(unit + large-gen)*
| Edge case | Test | Assertion |
|---|---|---|
| over per-attachment cap | `test_over_attach_cap_stripped` | `attachments_stripped == 1`, message emitted |
| over message cap | `test_over_msg_cap_dead_lettered` | `dead_lettered == 1` |
| allowlist blocks non-listed | `test_allowlist_blocks_non_listed` | quarantine/DLQ |
| allowlist case-insensitive | `test_allowlist_case_insensitive` | `application/PDF` matches |
| 30 MB attachment | `test_huge_attachment_no_oom` | processes, memory stable |

### F09 — Cleaning + stages  *(unit + integration)*
| Edge case | Test | Assertion |
|---|---|---|
| clean_fn before stages | `test_clean_fn_before_stages` | order observed |
| stage transforms email | `test_stage_transforms` | mutation applied |
| cleaner raises | `test_cleaner_raise_contained` | contained |

### F10 — Emit / sinks / projection  *(unit)*
| Edge case | Test | Assertion |
|---|---|---|
| field projection subset | `test_projection_subset` | dict has only requested fields |
| unknown projection field | `test_projection_unknown_field_raises` | `ValueError` |
| `from` alias serialization | `test_from_alias_serialized` | `"from"` key (not `from_`) |
| long message_id (SB) | `test_servicebus_hashes_long_id` | hashed when > 128 chars |

### F11 — Cursor  *(property + unit)*
| Edge case | Test | Assertion |
|---|---|---|
| advance on emit | `test_cursor_advances_on_emit` | cursor moved |
| advance on drop | `test_cursor_advances_on_drop` | cursor moved |
| advance on dead_letter | `test_cursor_advances_on_dead_letter` | cursor moved |
| advance on duplicate | `test_cursor_advances_on_duplicate` | cursor moved |
| reject regression | `test_commit_if_ahead_rejects_regression` | lower order ignored |

### F12 — DLQ / redrive  *(seam)*
| Edge case | Test | Assertion |
|---|---|---|
| one poison → one count | `test_one_poison_one_dlq_count` | `dead_lettered == 1`, dlq len 1 |
| record carries raw | `test_dlq_record_carries_raw` | `raw_b64` present |
| re-dead-letter idempotent | `test_re_dead_letter_idempotent` | overwrite by `record_id` |

### F13 — Error ladder  *(unit + fault-inject)*
| Edge case | Test | Assertion |
|---|---|---|
| transient then success | `test_transient_then_success` | run1 emit 0 + cursor unmoved; run2 emit 1 |
| transient exhausts | `test_transient_exhausts_to_dlq` | `dead_lettered == 1` |
| permanent → DLQ | `test_permanent_to_dlq` | straight to DLQ |
| auth refresh + retry once | `test_auth_refresh_retry_once` | one refresh, then emit |

### F14 — State (sqlite)  *(e2e restart)*
| Edge case | Test | Assertion |
|---|---|---|
| sqlite cursor persists | `test_sqlite_cursor_persists` | second run resumes |
| dedupe persists (no re-emit) | `test_sqlite_dedupe_no_reemit` | processed not re-emitted |

### F15 — Providers & transports  *(integration)*
| Edge case | Test | Assertion |
|---|---|---|
| forged clientState | `test_forged_client_state_dropped` | no emit |
| 404 on fetch | `test_404_graceful` | graceful skip |
| 500 on fetch | `test_500_abandon_reraise` | abandon + re-raise |
| EH == SB parity | `test_eventhub_servicebus_parity` | identical CleanEmail |
| empty batch | `test_empty_batch` | no-op, no crash |

### F16 — Live reliability  *(unit + subprocess)*
| Edge case | Test | Assertion |
|---|---|---|
| renew predicate | `test_should_schedule_renew` | true/false at boundaries |
| subprocess restart resumes | `test_subprocess_restart_resumes` *(slow)* | no duplicate emit |

### F17 — Fetch-by-id  *(integration)*
| Edge case | Test | Assertion |
|---|---|---|
| maps fields | `test_get_email_maps_fields` | CleanEmail correct |
| accessors | `test_get_body_recipients_attachments` | project right fields |
| memory provider | `test_memory_get_email_not_implemented` | `NotImplementedError` |

### Scenarios & invariants
| Scenario | Test | Assertion |
|---|---|---|
| bulk 10k | `test_bulk_10k_emitted_once` *(slow)* | `emitted == 10_000`, unique ids |
| duplicate ×5 | `test_same_id_x5_emits_once` | 1 emit |
| restart resume (sqlite) | `test_restart_resumes_without_reemit` | `duplicates==5, emitted==3` |
| restart (subprocess) | `test_kill_restart_subprocess` *(slow)* | no dup emit |
| large 30 MB attach | `test_30mb_attachment_stripped` | `attachments_stripped==1` |
| large 60 MB message | `test_60mb_message_dead_lettered` | `dead_lettered==1` |
| exactly-once + monotone | `test_exactly_once_and_cursor_monotone` | invariant holds for all orderings |
| parser fuzz | `test_parser_never_crashes_on_random_bytes` | only known error types |

---

# PART B — Phased execution

## PHASE 0 — Foundation (harness)

### Task 0.1: Store & sink fixtures — `tests/conftest.py`
- [ ] **Write:**
```python
import pytest
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore, InMemoryBlobStore
from mailflow.emit.memory import MemoryEmitter

@pytest.fixture
def mem_stores():
    return dict(cursor_store=InMemoryCursorStore(),
                dedupe_store=InMemoryDedupeStore(), blob_store=InMemoryBlobStore())

@pytest.fixture
def sink():
    return MemoryEmitter()
```
- [ ] `python -m pytest --collect-only` → no error. Commit.

### Task 0.2: Email builder — `tests/_harness/email_builder.py`
- [ ] **Failing test** `tests/qa/test_harness.py`:
```python
from tests._harness.email_builder import Email
def test_builder_both_shapes():
    e = Email(msg_id="M1", sender="a@partner.com", subject="hi", body="hello")
    assert b"Subject: hi" in e.as_rfc822()
    assert e.as_graph_json()["subject"] == "hi"
```
- [ ] Run → fails. **Implement** the `Email` dataclass with `.as_rfc822() -> bytes` and `.as_graph_json() -> dict` (and a `raw(msg_id, **kw)` shortcut) as specified in File Structure. Run → passes. Commit.

### Task 0.3: Corpus generator — `tests/_harness/corpus.py`
- [ ] **Failing test:** `bulk_seed(500, S)` has 500 entries; `large_attachment_raw(1_000_000)` > 1 MB.
- [ ] **Implement** `bulk_seed(n, stream)`, `large_attachment_raw(nbytes)`, and `TRICKY` (no-Message-ID / missing-From / html-only raw bytes). Run → passes. Commit.

### Task 0.4: Pipeline builder + fault injection — `tests/_harness/fakes.py`
- [ ] **Failing test:** `build_memory_pipeline(seed=bulk_seed(3,S), emitter=sink).run_once().emitted == 3`.
- [ ] **Implement** `build_memory_pipeline(*, seed, emitter=None, filters=None, on_filtered="tag", max_message_bytes=50_000_000, attachments=None, extractor=None, observers_on_trace=None, stores=None)` wrapping `Pipeline` + `MemoryProvider` + defaults; `FaultExtractor(fail_until)` raising `TransientError` on early attempts; and copy the Graph fake transport + `_graph_message` from `tests/providers/servicebus/test_e2e.py` for provider tests. Run → passes. Commit.

## PHASE 1 — Core pipeline (F01–F04, F11–F13)

One file per functionality; implement every test named in Part A for that F. Representative full test per file, then the rest follow the same pattern.

### Task 1.1: `tests/qa/test_f01_dedupe.py` — F01 (5 tests)
- [ ] Full pattern:
```python
from tests._harness.fakes import build_memory_pipeline
from tests._harness.email_builder import raw
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryDedupeStore, InMemoryCursorStore
S = StreamRef(mailbox="ops@acme.com", folder="inbox")

def test_same_message_twice_emits_once(sink):
    ded = InMemoryDedupeStore(); seed = {S: [SeedEmail("m1", raw("m1"))]}
    build_memory_pipeline(seed=seed, emitter=sink,
        stores=dict(dedupe_store=ded, cursor_store=InMemoryCursorStore())).run_once()
    r2 = build_memory_pipeline(seed=seed, emitter=sink,
        stores=dict(dedupe_store=ded, cursor_store=InMemoryCursorStore())).run_once()
    assert len(sink.events) == 1 and r2.duplicates == 1
```
- [ ] Add the other 4 F01 tests from the map. Run file → green. Commit.

### Task 1.2: `tests/qa/test_f02_size.py` — F02 (4 tests)
- [ ] Representative = `test_oversized_dead_letters_without_extracting` (spy extractor never called). Add the boundary tests. Commit.

### Task 1.3: `tests/qa/test_f03_parse.py` — F03 (6 tests)
- [ ] Parametrize over `TRICKY` + explicit header cases. Commit.

### Task 1.4: `tests/qa/test_f04_identity.py` — F04 (5 tests). Commit.
### Task 1.5: `tests/qa/test_f11_cursor.py` — F11 (5 tests). Commit.
### Task 1.6: `tests/qa/test_f12_dlq.py` — F12 (3 tests). Commit.
### Task 1.7: `tests/qa/test_f13_errors.py` — F13 (4 tests, uses `FaultExtractor`). Commit.

## PHASE 2 — Content path (F05–F09)

### Task 2.1: `tests/qa/test_f05_filter.py` — F05 (6 tests)
- [ ] Representative:
```python
def test_drop_reports_dropped_and_suppresses(sink):
    from mailflow.filters.deterministic import BlacklistFilter
    traces = []
    r = build_memory_pipeline(seed={S:[SeedEmail("m1", raw("m1"))]}, emitter=sink,
            filters=[BlacklistFilter({"partner.com"})], on_filtered="drop",
            observers_on_trace=traces.append).run_once()
    assert sink.events == [] and r.dropped == 1
    assert traces[0].matched_filter == "blacklist" and traces[0].reason
```
- [ ] Add the rest. Commit.

### Task 2.2: `tests/qa/test_f06_classifier.py` — F06 (3 tests). Commit.
### Task 2.3: `tests/qa/test_f07_extract.py` — F07 (5 tests, incl. `test_gmail_graph_parity`). Commit.
### Task 2.4: `tests/qa/test_f08_attachments.py` — F08 (5 tests, uses `large_attachment_raw`). Commit.
### Task 2.5: `tests/qa/test_f09_stages.py` — F09 (3 tests, via `connect()`). Commit.

## PHASE 3 — I/O, state, providers (F10, F14, F15, F17)

### Task 3.1: `tests/qa/test_f10_emit.py` — F10 (4 tests). Commit.
### Task 3.2: `tests/qa/test_f14_state.py` — F14 (2 tests, sqlite). Commit.
### Task 3.3: `tests/qa/test_f15_providers.py` — F15 (5 tests, Graph fake). Commit.
### Task 3.4: `tests/qa/test_f17_fetch.py` — F17 (3 tests). Commit.

## PHASE 4 — Scenarios

### Task 4.1: `tests/qa/test_scenarios_bulk.py` — `test_bulk_10k_emitted_once` (`@pytest.mark.slow`). Commit.
### Task 4.2: `tests/qa/test_scenarios_dup.py` — `test_same_id_x5_emits_once` + interleaved streams. Commit.
### Task 4.3: `tests/qa/test_scenarios_restart.py`
- [ ] Full sqlite-restart test:
```python
from mailflow import connect
def test_restart_resumes_without_reemit(tmp_path):
    db = f"sqlite:///{tmp_path/'mf.db'}"
    all8 = {S: [SeedEmail(f"m{i}", raw(f"m{i}")) for i in range(8)]}
    connect("memory", seed={S: all8[S][:5]}, state=db).fetch_new()
    caps = []; connect("memory", seed=all8, state=db, on_report=caps.append).fetch_new()
    assert caps[0].duplicates == 5 and caps[0].emitted == 3
```
- [ ] Add `test_kill_restart_subprocess` (`@pytest.mark.slow`, spawns runner, kills, restarts, checks log/db). Commit.

### Task 4.4: `tests/qa/test_scenarios_large.py` — `test_30mb_attachment_stripped`, `test_60mb_message_dead_lettered`. Commit.

## PHASE 5 — Property & fuzz

### Task 5.1: `tests/qa/test_property_invariants.py`
- [ ] Add `hypothesis` to dev deps. Implement `test_exactly_once_and_cursor_monotone` (generated dup/reorder sequences → each id emitted once + cursor monotone) and `test_parser_never_crashes_on_random_bytes` (`st.binary()` into `parse_envelope`). Commit.

### Task 5.2: Markers + CI note
- [ ] Register `slow`/`live` markers in `pyproject.toml`; append a coverage summary to `docs/qa-findings.md`:
  - commit: `pytest -m "not slow and not live"` + `mypy`
  - nightly: `-m slow` (bulk/restart/property)
  - gated: `-m live`
- [ ] Commit.

---

## Self-Review

**Coverage:** Part A enumerates every edge case → test name → assertion for F01–F17 (F18 already done); Part B implements them phase by phase, plus scenarios and property. **Tooling:** pytest + hypothesis only — no Playwright (headless library). **Placeholders:** harness + the tricky tests are full code; every other test is a named entry in Part A bound to the pattern shown in its task. **Types/naming:** `build_memory_pipeline`, `Email`, `raw`, `bulk_seed`, `large_attachment_raw`, `FaultExtractor` are used consistently across phases.

---

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-07-06-qa-automation.md`. Two options:
1. **Subagent-Driven (recommended)** — fresh subagent per task, review between each, local-only.
2. **Inline Execution** — batch with checkpoints.

Which approach? (Phase 0 first — it unlocks every later phase.)
