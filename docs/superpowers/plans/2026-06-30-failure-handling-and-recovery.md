# Failure Handling & Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the two P1 fast-follow failure-handling features for mailflow V1 — a durable, replayable DLQ with a documented redrive path, and TRUE 401→refresh→retry-once / 403→permanent auth semantics — without breaking any frozen §8 invariant.

**Architecture:** Two independent tracks over the existing per-message spine in `core/pipeline.py`. (1) DLQ replayability adds a durable `DeadLetterRecord` written *alongside* (never instead of) the existing `dlq_emitter` + `add_dead_letter()` paired write, plus a `DeadLetterStore` port, in-memory + sqlite adapters, and a `redrive()` entrypoint that rebuilds the original `RawMessage` and re-runs it through a fresh pipeline. (2) Auth correctness splits the error taxonomy by *retry style*: `AuthError` gets a dedicated in-process **refresh-and-retry-once** path (bounded by a boolean, never by `max_attempts`), while `TransientError`/generic exceptions keep the bounded `max_attempts` release/reclaim loop and `PermanentError` keeps immediate DLQ-no-retry. A complementary 401-refresh-retry-once also lands in `GmailClient` for the Gmail fetch-time auth surface.

**Tech Stack:** Python 3.12, pydantic 2.13, pytest 9, mypy (strict), sqlite3 (stdlib), base64 (stdlib). PyYAML is NOT installed — keep any config fixtures JSON-compatible.

## Global Constraints

- **Interpreter (tests/types):** `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest` and `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` (mypy configured `packages = ["mailflow"]`, strict). **mypy must be clean on every commit** and the tree must stay importable (commit a module before the module that imports it).
- **Frozen DLQ contract:** `RunReport.dead_lettered` is incremented **only** by `add_dead_letter()`; `record()` does NOT count it. `Pipeline._dead_letter` must keep calling `add_dead_letter()` **and** `record(_trace(...dead_lettered...))` as a paired unit — one poison/oversized message ⇒ **exactly one** count. The new durable write is purely **additive** and must not touch any counter.
- **Frozen cursor contract:** `commit_if_ahead` is strictly monotonic; the cursor advances on **every** terminal disposition (emitted/dropped/duplicate/dead_lettered) and only on a terminal disposition. The refresh-once change must not advance the cursor on a non-terminal AuthError, and must not double-advance.
- **Frozen dedupe shape:** exactly `try_claim / record_attempt / mark_done / release`. Do NOT add methods to the `DedupeStore` port. (Redrive sidesteps the marked-done claim by running on a fresh, redrive-scoped `DedupeStore`.)
- **Frozen identity:** `idempotency_key = (tenant, mailbox, provider_message_id)`; reused verbatim as the durable DLQ `record_id` so a re-dead-letter overwrites rather than duplicates.
- **Commit trailer (every commit):** end the message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. **Never `git push`.**
- **CONTRACT DECISION (reconciling "retry-once" with `max_attempts`):** the pre-existing bug is that `AuthError` shared the `TransientError` `_retry_or_dead_letter(max_attempts)` loop, so a 401 retried up to 3× via release/reclaim. The fix **routes `AuthError` out of that loop entirely** into `_handle_auth_error`, which forces one refresh and retries the work body exactly once in the same call (bounded by straight-line control flow, not a counter). `max_attempts` is therefore left untouched and continues to govern **only** transient/generic retries. Downstream impact: `TransientError` semantics are unchanged; the two existing AuthError routing tests in `tests/test_pipeline_routing.py` assert the *old* loop behavior and are rewritten in Task 2.

---

### Task 1: `OAuthTokenProvider.force_refresh()` — re-mint the access token after a 401

**Files:**
- Modify: `src/mailflow/adapters/gmail/live.py:76-87` (refactor `get_token`, add `force_refresh`)
- Test: `tests/test_gmail_token_rotation.py` (append)

**Interfaces:**
- Produces: `OAuthTokenProvider.force_refresh(self) -> None` — unconditionally refreshes the credential and runs the A8 rotation-sink path. Makes `OAuthTokenProvider` satisfy both the `AuthRefresher` port (Task 2) and the `RefreshableTokenProvider` protocol (Task 3).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gmail_token_rotation.py`:

```python
def test_force_refresh_remints_and_rotates() -> None:
    creds = _RotatingCreds(refresh_token="old", new_refresh_token="new")
    sink = _FakeSink()
    provider = _provider(creds, sink)
    provider.force_refresh()
    assert creds.token == "access-token"
    assert sink.calls == [("secret/refresh", "new")]


def test_force_refresh_without_rotation_does_not_call_sink() -> None:
    creds = _RotatingCreds(refresh_token="old", new_refresh_token=None)
    sink = _FakeSink()
    provider = _provider(creds, sink)
    provider.force_refresh()
    assert sink.calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_gmail_token_rotation.py::test_force_refresh_remints_and_rotates -v`
Expected: FAIL with `AttributeError: 'OAuthTokenProvider' object has no attribute 'force_refresh'`.

- [ ] **Step 3: Write minimal implementation**

In `src/mailflow/adapters/gmail/live.py`, replace the `get_token` method (lines 76-87) with a shared helper plus `get_token` and `force_refresh`:

```python
    def _refresh_and_maybe_rotate(self) -> None:
        before = getattr(self._creds, "refresh_token", None)
        self._creds.refresh(self._new_request())
        after = getattr(self._creds, "refresh_token", None)
        if (
            self._rotation_sink is not None
            and after is not None
            and after != before
        ):
            self._rotation_sink.on_refresh(self._refresh_token_ref, str(after))

    def get_token(self) -> str:
        if not self._creds.valid:
            self._refresh_and_maybe_rotate()
        return str(self._creds.token)

    def force_refresh(self) -> None:
        """A2: unconditionally re-mint the access token after a 401. The cached token may
        look locally-valid but be server-rejected, so this ignores `.valid`. Honors the
        same A8 rotation-sink path as `get_token`."""
        self._refresh_and_maybe_rotate()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_gmail_token_rotation.py -v`
Expected: PASS (the two new tests plus the three pre-existing rotation tests stay green — `get_token` behavior is unchanged).

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/gmail/live.py tests/test_gmail_token_rotation.py
git commit -m "feat(auth): add OAuthTokenProvider.force_refresh for post-401 re-mint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Pipeline 401→refresh→retry-once routing (the core fix)

**Files:**
- Modify: `src/mailflow/core/ports.py` (add `AuthRefresher` port after `MailboxProvider`)
- Modify: `src/mailflow/core/pipeline.py:50-78` (add `auth_refresher` param), `:96-177` (extract `_do_work`, add `_handle_auth_error`, re-route `AuthError`)
- Modify: `src/mailflow/core/errors.py:29-30` (docstring → real behavior)
- Test: `tests/test_pipeline_routing.py` (rewrite the two AuthError tests)

**Interfaces:**
- Consumes: `OAuthTokenProvider.force_refresh()` (Task 1).
- Produces: `AuthRefresher` Protocol with `force_refresh(self) -> None`; `Pipeline(..., auth_refresher: AuthRefresher | None = None)`; private `Pipeline._do_work(self, msg: RawMessage, key: str, report: RunReport) -> Disposition` and `Pipeline._handle_auth_error(self, canonical_id, msg, key, report, attempts, exc) -> Disposition`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_pipeline_routing.py`, add the imports and helpers near the top (after the existing imports):

```python
from mailflow.core.events import SCHEMA_VERSION
from mailflow.core.models import CleanEmail
from mailflow.stores.memory import InMemoryCursorStore


class SpyRefresher:
    def __init__(self) -> None:
        self.calls = 0

    def force_refresh(self) -> None:
        self.calls += 1


def _ok_email(msg: RawMessage, env: Envelope) -> CleanEmail:
    return CleanEmail(
        canonical_id=env.canonical_id,
        provider=msg.provider,
        provider_message_id=msg.provider_message_id,
        provider_stream_id=msg.stream.key,
        schema_version=SCHEMA_VERSION,
    )


class FlakyAuthExtractor:
    """Raises AuthError on the first extract, succeeds on the second (post-refresh)."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, msg: RawMessage, env: Envelope) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise AuthError("401")
        return _ok_email(msg, env)
```

Extend `build()` to accept an `auth_refresher`:

```python
def build(*, extractor: Any, max_attempts: int = 3,
          cursor_store: Any = None,
          auth_refresher: Any = None) -> tuple[Pipeline, MemoryEmitter, MemoryEmitter]:
    emit, dlq = MemoryEmitter(), MemoryEmitter()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=extractor,
        emitter=emit, dlq_emitter=dlq,
        cursor_store=cursor_store or InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme", max_attempts=max_attempts),
        auth_refresher=auth_refresher,
    )
    return pipe, emit, dlq
```

Replace the two existing AuthError tests (`test_auth_error_retries_while_attempts_remain` and `test_auth_error_dead_letters_at_max_attempts`) with:

```python
def test_auth_error_refreshes_then_retry_succeeds() -> None:
    extractor = FlakyAuthExtractor()
    refresher = SpyRefresher()
    pipe, emit, dlq = build(extractor=extractor, max_attempts=5, auth_refresher=refresher)
    r = pipe.run_once()
    assert r.emitted == 1 and r.dead_lettered == 0
    assert refresher.calls == 1          # forced exactly one refresh
    assert extractor.calls == 2          # retried exactly once
    assert len(dlq.events) == 0


def test_auth_error_dead_letters_after_one_retry_not_max_attempts() -> None:
    # max_attempts high: a persistent 401 must NOT loop on max_attempts. It refreshes
    # ONCE, retries ONCE, then dead-letters (counted once, cursor advances).
    cur = InMemoryCursorStore()
    refresher = SpyRefresher()
    pipe, emit, dlq = build(
        extractor=RaisingExtractor(AuthError("401")), max_attempts=5,
        cursor_store=cur, auth_refresher=refresher,
    )
    r = pipe.run_once()
    assert r.emitted == 0 and r.dead_lettered == 1 and len(dlq.events) == 1
    assert refresher.calls == 1                       # exactly one refresh, no loop
    assert cur.get("acme", STREAM) is not None        # terminal -> cursor advanced


def test_auth_error_without_refresher_still_retries_once_then_dlq() -> None:
    pipe, emit, dlq = build(extractor=RaisingExtractor(AuthError("401")), max_attempts=5)
    r = pipe.run_once()
    assert r.dead_lettered == 1 and len(dlq.events) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_pipeline_routing.py::test_auth_error_refreshes_then_retry_succeeds -v`
Expected: FAIL — `Pipeline.__init__() got an unexpected keyword argument 'auth_refresher'`.

- [ ] **Step 3a: Add the `AuthRefresher` port**

In `src/mailflow/core/ports.py`, add after the `MailboxProvider` block (after line 49):

```python
@stable
@runtime_checkable
class AuthRefresher(Protocol):
    """Forces a credential refresh after a 401 so the in-process AuthError retry
    re-authenticates with a fresh token (A2)."""

    def force_refresh(self) -> None: ...
```

- [ ] **Step 3b: Wire the pipeline**

In `src/mailflow/core/pipeline.py`, import the port (add `AuthRefresher` to the `mailflow.core.ports` import list at lines 27-37):

```python
from mailflow.core.ports import (
    AuthRefresher,
    BlobStore,
    Classifier,
    ContentCleaner,
    ContentExtractor,
    CursorStore,
    DedupeStore,
    Emitter,
    EnvelopeParser,
    MailboxProvider,
)
```

Add the constructor parameter (after `cleaner: ContentCleaner | None = None,` at line 65) and store it:

```python
        cleaner: ContentCleaner | None = None,
        auth_refresher: AuthRefresher | None = None,
    ) -> None:
```

```python
        self.cleaner = cleaner
        self.auth_refresher = auth_refresher
```

Extract the work body. Replace the `try: ... except Exception` block at lines 128-177 with a delegating try plus a new `_do_work` and `_handle_auth_error`:

```python
        try:
            return self._do_work(msg, key, report)
        except PermanentError as exc:
            # §A2: will never succeed (403/404/410, invalid base64) -> DLQ, no retry.
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"{type(exc).__name__}: {exc}",
            )
        except AuthError as exc:
            # §A2: 401 -> force ONE token refresh, retry the message EXACTLY once, else DLQ.
            return self._handle_auth_error(canonical_id, msg, key, report, attempts, exc)
        except TransientError as exc:
            # §A2: temporary (429/5xx/network) -> bounded max_attempts retry, else DLQ.
            return self._retry_or_dead_letter(canonical_id, msg, key, report, attempts, exc)
        except Exception as exc:  # noqa: BLE001 - unknown failures are treated as transient
            return self._retry_or_dead_letter(canonical_id, msg, key, report, attempts, exc)

    def _do_work(self, msg: RawMessage, key: str, report: RunReport) -> Disposition:
        tenant = self.config.tenant
        env = self.parser.parse_envelope(msg, tenant)
        decision = self.filters.run(env, FilterContext(tenant=tenant))

        if decision.decision is Decision.drop:
            self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
            report.record(self._trace(
                env.canonical_id, msg, Disposition.dropped, "filter",
                matched_filter=decision.filter_name, reason=decision.reason,
            ))
            return Disposition.dropped

        relevance = None
        if self.classifier is not None and decision.decision is Decision.uncertain:
            relevance = self.classifier.classify(env, FilterContext(tenant=tenant))

        email = self._extract(msg, env)
        if relevance is not None:
            email.relevance = relevance
        email.matched_filter = decision.filter_name

        event = EmailEvent(
            schema_version=SCHEMA_VERSION,
            tenant=tenant,
            ordering_key=msg.stream.mailbox,
            idempotency_key=key,  # §A4: (tenant, mailbox, provider_message_id) on the wire
            email=email,
        )
        self.emitter.emit(event)
        self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
        report.record(self._trace(
            email.canonical_id, msg, Disposition.emitted, "emit",
            relevance_score=(relevance.score if relevance else None),
        ))
        return Disposition.emitted

    def _handle_auth_error(
        self, canonical_id: str, msg: RawMessage, key: str, report: RunReport,
        attempts: int, exc: Exception,
    ) -> Disposition:
        # §A2 refresh-and-retry-ONCE: force one credential refresh, then retry the work
        # body a single time in THIS call. Bounded by straight-line control flow (not a
        # counter), so it can never loop on max_attempts. A second failure dead-letters.
        if self.auth_refresher is not None:
            self.auth_refresher.force_refresh()
        try:
            return self._do_work(msg, key, report)
        except Exception as retry_exc:  # noqa: BLE001 - one shot only; any failure -> DLQ
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"auth retry failed after refresh: "
                       f"{type(retry_exc).__name__}: {retry_exc}",
            )
```

> Note: on the failing first attempt, `_do_work` raises in `_extract` (before `mark_done`/`record`), so the claim is still held and no trace was recorded — the retry re-runs the pure parse/filter and re-extracts cleanly; on success exactly one emit trace is recorded. A re-emit of an event already sent before the failure is at-least-once-safe (downstream dedupes by `idempotency_key`, §A4).

- [ ] **Step 3c: Fix the errors.py docstring**

In `src/mailflow/core/errors.py`, update the `AuthError` docstring (line 30):

```python
class AuthError(MailflowError):
    """Login/permission failure (e.g. 401). Routing: force one token refresh, retry the
    message exactly once, else DLQ — never a max_attempts loop (A2)."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_pipeline_routing.py -v`
Expected: PASS (all routing tests, including the unchanged Permanent/Transient/generic ones).

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/core/ports.py src/mailflow/core/pipeline.py src/mailflow/core/errors.py tests/test_pipeline_routing.py
git commit -m "fix(auth): route AuthError to refresh-and-retry-once, not the max_attempts loop

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: GmailClient 401→refresh→retry-once for the fetch-time auth surface

**Files:**
- Modify: `src/mailflow/adapters/gmail/transport.py:72-74` (add `RefreshableTokenProvider` protocol)
- Modify: `src/mailflow/adapters/gmail/client.py:12-19` (import), `:56-87` (`_request_inner` 401 branch)
- Test: `tests/test_gmail_client_errors.py` (append)

**Interfaces:**
- Consumes: `OAuthTokenProvider.force_refresh()` (Task 1).
- Produces: `RefreshableTokenProvider` Protocol (`get_token` + `force_refresh`). `GmailClient` does one forced refresh + one retry on a 401, bounded by a per-request boolean independent of `max_retries`.

> Why this exists: Gmail's 401 surfaces inside `provider.fetch()` (`client.get_message_raw`), which runs in `_run_stream` *outside* `_process`, so the Task-2 pipeline handler never sees it. This adapter-local handler closes that gap; the two are at different layers and each is independently bounded to one retry.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gmail_client_errors.py`:

```python
import pytest

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.transport import GmailAuthError


class _Resp:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code = status
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    @property
    def headers(self) -> dict[str, str]:
        return {}

    @property
    def content(self) -> bytes:
        return b""


class _SeqTransport:
    def __init__(self, statuses: list[int]) -> None:
        self._statuses = list(statuses)
        self.auth_headers: list[str] = []

    def request(self, method: str, url: str, *, headers: dict[str, str], json: object) -> _Resp:
        self.auth_headers.append(headers["Authorization"])
        return _Resp(self._statuses.pop(0), {"ok": True})


class _RefreshableTokens:
    def __init__(self) -> None:
        self.token = "t0"
        self.refreshes = 0

    def get_token(self) -> str:
        return self.token

    def force_refresh(self) -> None:
        self.refreshes += 1
        self.token = f"t{self.refreshes}"


def test_client_refreshes_and_retries_once_on_401() -> None:
    tokens = _RefreshableTokens()
    transport = _SeqTransport([401, 200])
    client = GmailClient(
        base_url="https://g", token_provider=tokens, transport=transport, max_retries=0,
    )
    client.get_profile("me@x")
    assert tokens.refreshes == 1
    assert transport.auth_headers == ["Bearer t0", "Bearer t1"]  # retried with fresh token


def test_client_raises_auth_error_after_second_401_no_loop() -> None:
    tokens = _RefreshableTokens()
    client = GmailClient(
        base_url="https://g", token_provider=tokens,
        transport=_SeqTransport([401, 401]), max_retries=0,
    )
    with pytest.raises(GmailAuthError):
        client.get_profile("me@x")
    assert tokens.refreshes == 1  # exactly one refresh, no loop
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_gmail_client_errors.py::test_client_refreshes_and_retries_once_on_401 -v`
Expected: FAIL — the first 401 is mapped straight to `GmailAuthError` (no refresh/retry), so `tokens.refreshes == 0`.

- [ ] **Step 3a: Add the protocol**

In `src/mailflow/adapters/gmail/transport.py`, replace the `TokenProvider` block (lines 72-74) with:

```python
@runtime_checkable
class TokenProvider(Protocol):
    def get_token(self) -> str: ...


@runtime_checkable
class RefreshableTokenProvider(Protocol):
    """A TokenProvider that can force a credential refresh after a 401 (A2)."""

    def get_token(self) -> str: ...
    def force_refresh(self) -> None: ...
```

- [ ] **Step 3b: Handle 401 in the client**

In `src/mailflow/adapters/gmail/client.py`, add `RefreshableTokenProvider` to the transport import (lines 12-19):

```python
from mailflow.adapters.gmail.transport import (
    GmailError,
    HttpResponse,
    HttpTransport,
    RefreshableTokenProvider,
    StaleHistoryError,
    TokenProvider,
    gmail_error_for,
)
```

Replace `_request_inner` (lines 56-87) with the 401-aware version:

```python
    def _request_inner(self, method: str, url: str, *, json: Any | None) -> HttpResponse:
        attempt = 0
        auth_retried = False
        while True:
            headers = {
                "Authorization": f"Bearer {self.tokens.get_token()}",
                "Content-Type": "application/json",
            }
            try:
                resp = self.transport.request(method, url, headers=headers, json=json)
            except Exception as exc:  # noqa: BLE001 - network failure is transient
                if attempt < self.max_retries:
                    self._backoff_sleep(attempt, None)
                    attempt += 1
                    continue
                raise TransientError(f"gmail network error: {exc}") from exc
            status = resp.status_code
            # B4: retry 429 AND 5xx with bounded backoff; after the bound the typed
            # mapping turns the final 429/5xx into a TransientError (DLQ-or-retry upstream).
            if (status == 429 or status >= 500) and attempt < self.max_retries:
                self._backoff_sleep(attempt, resp.headers.get("Retry-After"))
                attempt += 1
                continue
            # A2: a 401 means the access token was rejected. Force ONE refresh and retry
            # the request a single time with a fresh bearer token (bounded by auth_retried,
            # independent of max_retries); a second 401 propagates as GmailAuthError.
            if (
                status == 401
                and not auth_retried
                and isinstance(self.tokens, RefreshableTokenProvider)
            ):
                self.tokens.force_refresh()
                auth_retried = True
                continue
            if status >= 400:
                message = ""
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        message = str(body.get("error", {}).get("message", ""))
                except Exception:  # noqa: BLE001 - error body may not be JSON
                    message = ""
                raise gmail_error_for(status, message)
            return resp
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_gmail_client_errors.py tests/test_gmail_client_backoff.py -v`
Expected: PASS (new 401 tests + unchanged 429/5xx backoff tests).

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/gmail/transport.py src/mailflow/adapters/gmail/client.py tests/test_gmail_client_errors.py
git commit -m "fix(gmail): force one token refresh + retry once on a fetch-time 401

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `DeadLetterRecord` model + `DeadLetterStore` port

**Files:**
- Modify: `src/mailflow/core/observability.py:5-26` (add `datetime`/`SCHEMA_VERSION` imports + `DeadLetterRecord`)
- Modify: `src/mailflow/core/ports.py` (add `DeadLetterStore` port)
- Test: `tests/core/test_deadletter_record.py` (create)

**Interfaces:**
- Produces: `DeadLetterRecord` (durable, replayable pydantic model) and `DeadLetterStore` Protocol (`put`, `list_pending`, `delete`). Consumed by Tasks 5/6/7/8.

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_deadletter_record.py`:

```python
"""DeadLetterRecord captures everything needed to rebuild and replay the original
RawMessage: provider ids, stream, raw bytes (base64), failure reason + class, and the
attempt count."""

from __future__ import annotations

from datetime import datetime, timezone

from mailflow.core.observability import DeadLetterRecord


def test_dead_letter_record_round_trips_via_json() -> None:
    now = datetime(2026, 6, 30, tzinfo=timezone.utc)
    rec = DeadLetterRecord(
        record_id="acme|ops@acme.com|m1",
        tenant="acme",
        provider="gmail",
        provider_message_id="m1",
        mailbox="ops@acme.com",
        folder="Inbox",
        canonical_id="<m1@x>",
        reason="PermanentError: invalid base64",
        error_class="PermanentError",
        attempts=2,
        size_bytes=42,
        thread_key="t1",
        cursor_value="ops@acme.com#1",
        cursor_order=1,
        received_at=now,
        dead_lettered_at=now,
        raw_b64="aGVsbG8=",
    )
    restored = DeadLetterRecord.model_validate_json(rec.model_dump_json())
    assert restored == rec
    assert restored.record_id == "acme|ops@acme.com|m1"
    assert restored.raw_b64 == "aGVsbG8="
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/core/test_deadletter_record.py -v`
Expected: FAIL — `ImportError: cannot import name 'DeadLetterRecord'`.

- [ ] **Step 3a: Add the model**

In `src/mailflow/core/observability.py`, update the imports (lines 5-9) and add the model after `DeadLetter` (after line 26):

```python
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from mailflow.core.events import SCHEMA_VERSION
from mailflow.core.models import Disposition
```

```python
class DeadLetterRecord(BaseModel):
    """A durable, replayable dead-letter: everything needed to rebuild the original
    RawMessage and re-submit it through the pipeline once the root cause is fixed.

    `record_id` is the idempotency_key (tenant, mailbox, provider_message_id), so a
    re-dead-letter of the same message overwrites rather than duplicates. `raw_b64` is
    the base64 of the original RFC822 bytes (may be empty if the message had none)."""

    record_id: str
    tenant: str
    provider: str
    provider_message_id: str
    mailbox: str
    folder: str | None = None
    canonical_id: str
    reason: str
    error_class: str = ""
    attempts: int = 0
    size_bytes: int = 0
    thread_key: str = ""
    cursor_value: str = ""
    cursor_order: int = 0
    received_at: datetime
    dead_lettered_at: datetime
    raw_b64: str = ""
    schema_version: str = SCHEMA_VERSION
```

> Note: `observability.py` already imports from `core.models`; adding `core.events` (which imports only `core.models`) introduces no cycle, and `core.ports` does not import `observability` today (see Step 3b for the new edge).

- [ ] **Step 3b: Add the port**

In `src/mailflow/core/ports.py`, add the import near the top (after the `mailflow.core.models` import block, line 22):

```python
from mailflow.core.observability import DeadLetterRecord
```

Add the port after the `BlobStore` block (after line 117):

```python
@stable
@runtime_checkable
class DeadLetterStore(Protocol):
    """Durable, replayable dead-letter records (A2 redrive). Separate from the
    fire-and-forget `dlq_emitter` sink: this one is queryable + deletable so an operator
    can redrive after fixing the root cause."""

    def put(self, record: DeadLetterRecord) -> None: ...
    def list_pending(self, *, limit: int | None = None) -> list[DeadLetterRecord]: ...
    def delete(self, record_id: str) -> None: ...
```

> Import-order check: `core.ports` already imports `core.events`, `core.filtering`, `core.models`; adding `core.observability` is safe because `observability` imports only `core.events`/`core.models` (no edge back to `ports`). Commit this together with Step 3a so the tree stays importable.

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/core/test_deadletter_record.py -v`
Expected: PASS.

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/core/observability.py src/mailflow/core/ports.py tests/core/test_deadletter_record.py
git commit -m "feat(dlq): add durable DeadLetterRecord model + DeadLetterStore port

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: In-memory + sqlite `DeadLetterStore` adapters

**Files:**
- Modify: `src/mailflow/stores/memory.py` (add `InMemoryDeadLetterStore`)
- Modify: `src/mailflow/stores/sqlite.py` (add `SqliteDeadLetterStore` + schema)
- Test: `tests/stores/test_deadletter_stores.py` (create)

**Interfaces:**
- Consumes: `DeadLetterRecord` (Task 4).
- Produces: `InMemoryDeadLetterStore` and `SqliteDeadLetterStore(db_path: str = "mailflow.db")`, both satisfying `DeadLetterStore`.

- [ ] **Step 1: Write the failing tests**

Create `tests/stores/test_deadletter_stores.py`:

```python
"""DeadLetterStore adapters: put is idempotent by record_id, list_pending is ordered +
limitable, delete removes, and the sqlite store survives a reopen (durability)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from mailflow.core.observability import DeadLetterRecord
from mailflow.stores.memory import InMemoryDeadLetterStore
from mailflow.stores.sqlite import SqliteDeadLetterStore


def _record(record_id: str, *, reason: str = "boom") -> DeadLetterRecord:
    now = datetime(2026, 6, 30, tzinfo=timezone.utc)
    return DeadLetterRecord(
        record_id=record_id, tenant="acme", provider="memory",
        provider_message_id=record_id, mailbox="ops@acme.com", folder="Inbox",
        canonical_id=f"<{record_id}@x>", reason=reason, error_class="PermanentError",
        attempts=1, size_bytes=5, thread_key="t", cursor_value="c", cursor_order=1,
        received_at=now, dead_lettered_at=now, raw_b64="aGk=",
    )


def test_inmemory_put_list_delete() -> None:
    store = InMemoryDeadLetterStore()
    store.put(_record("k1"))
    store.put(_record("k2"))
    assert [r.record_id for r in store.list_pending()] == ["k1", "k2"]
    assert [r.record_id for r in store.list_pending(limit=1)] == ["k1"]
    store.delete("k1")
    assert [r.record_id for r in store.list_pending()] == ["k2"]


def test_inmemory_put_idempotent_by_record_id() -> None:
    store = InMemoryDeadLetterStore()
    store.put(_record("k1", reason="a"))
    store.put(_record("k1", reason="b"))
    pending = store.list_pending()
    assert len(pending) == 1 and pending[0].reason == "b"


def test_sqlite_persists_across_reopen(tmp_path: Path) -> None:
    db = str(tmp_path / "dlq.db")
    SqliteDeadLetterStore(db).put(_record("k1"))
    reopened = SqliteDeadLetterStore(db)
    pending = reopened.list_pending()
    assert [r.record_id for r in pending] == ["k1"]
    assert pending[0].raw_b64 == "aGk="
    reopened.delete("k1")
    assert SqliteDeadLetterStore(db).list_pending() == []


def test_sqlite_put_idempotent_by_record_id(tmp_path: Path) -> None:
    db = str(tmp_path / "dlq.db")
    store = SqliteDeadLetterStore(db)
    store.put(_record("k1", reason="a"))
    store.put(_record("k1", reason="b"))
    pending = store.list_pending()
    assert len(pending) == 1 and pending[0].reason == "b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/stores/test_deadletter_stores.py -v`
Expected: FAIL — `ImportError: cannot import name 'InMemoryDeadLetterStore'`.

- [ ] **Step 3a: In-memory adapter**

In `src/mailflow/stores/memory.py`, add the import and the class (the file already imports from `core.models`; add the new import at the top, then append the class at the end):

```python
from mailflow.core.observability import DeadLetterRecord
```

```python
class InMemoryDeadLetterStore:
    """Durable-shape DLQ store for tests + the zero-setup path. Keyed by record_id
    (insertion-ordered) so put overwrites and list_pending is deterministic."""

    def __init__(self) -> None:
        self._records: dict[str, DeadLetterRecord] = {}

    def put(self, record: DeadLetterRecord) -> None:
        self._records[record.record_id] = record

    def list_pending(self, *, limit: int | None = None) -> list[DeadLetterRecord]:
        records = list(self._records.values())
        return records if limit is None else records[:limit]

    def delete(self, record_id: str) -> None:
        self._records.pop(record_id, None)
```

- [ ] **Step 3b: Sqlite adapter**

In `src/mailflow/stores/sqlite.py`, add the import (top) and schema constant (after `_DEDUPE_SCHEMA`), then append the class:

```python
from mailflow.core.observability import DeadLetterRecord
```

```python
_DEADLETTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS dead_letters (
  record_id TEXT PRIMARY KEY,
  payload   TEXT NOT NULL
);
"""
```

```python
class SqliteDeadLetterStore:
    """Durable, replayable DLQ records in a single SQLite table (one JSON payload per
    record_id). Mirrors the in-memory store method-for-method (DeadLetterStore port)."""

    def __init__(self, db_path: str = "mailflow.db") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(_DEADLETTER_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def put(self, record: DeadLetterRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO dead_letters (record_id, payload) VALUES (?, ?) "
                "ON CONFLICT(record_id) DO UPDATE SET payload=excluded.payload",
                (record.record_id, record.model_dump_json()),
            )
            self._conn.commit()

    def list_pending(self, *, limit: int | None = None) -> list[DeadLetterRecord]:
        sql = "SELECT payload FROM dead_letters ORDER BY rowid"
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [DeadLetterRecord.model_validate_json(str(row[0])) for row in rows]

    def delete(self, record_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM dead_letters WHERE record_id=?", (record_id,))
            self._conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/stores/test_deadletter_stores.py -v`
Expected: PASS.

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/stores/memory.py src/mailflow/stores/sqlite.py tests/stores/test_deadletter_stores.py
git commit -m "feat(dlq): in-memory + sqlite DeadLetterStore adapters

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Pipeline writes the durable record on dead-letter (additive, single-count)

**Files:**
- Modify: `src/mailflow/core/pipeline.py:10-12` (imports), `:50-78` (`dlq_store` param), `:96-126` (thread `attempts`/`error_class` to size-guard DLQs), `:128-177` (thread on routing DLQs), `:212-229` (`_dead_letter` writes the record), add `_dead_letter_record`
- Test: `tests/test_pipeline_dlq_durable.py` (create)

**Interfaces:**
- Consumes: `DeadLetterStore` (Task 4), `DeadLetterRecord` (Task 4).
- Produces: `Pipeline(..., dlq_store: DeadLetterStore | None = None)`; `_dead_letter(..., *, reason, attempts=0, error_class="")` now also writes a `DeadLetterRecord` when `dlq_store` is set. Counting is unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline_dlq_durable.py`:

```python
"""A dead-letter still counts exactly once (frozen DLQ contract) AND, when a
DeadLetterStore is wired, writes a durable replayable record carrying the raw bytes,
error class, and attempt count."""

from __future__ import annotations

from typing import Any

from mailflow.core.errors import PermanentError
from mailflow.core.models import Envelope, RawMessage, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDeadLetterStore,
    InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def raw(mid: str = "m1") -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: a@partner.com\r\n"
            f"To: ops@acme.com\r\nSubject: hi\r\n\r\nbody").encode()


class RaisingExtractor:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def extract(self, msg: RawMessage, env: Envelope) -> Any:
        raise self.exc


def test_dead_letter_writes_durable_record_and_counts_once() -> None:
    dlq_store = InMemoryDeadLetterStore()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=RaisingExtractor(PermanentError("nope")),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
        dlq_store=dlq_store,
    )
    r = pipe.run_once()
    assert r.dead_lettered == 1                      # frozen contract: counted once
    records = dlq_store.list_pending()
    assert len(records) == 1
    rec = records[0]
    assert rec.error_class == "PermanentError"
    assert rec.attempts >= 1
    assert rec.raw_b64 != ""                         # raw captured for replay
    assert rec.provider_message_id == "m1"
    assert rec.record_id == "acme|ops@acme.com|m1"
```

> The expected `record_id` is the verbatim `idempotency_key`. Confirm the separator your `idempotency_key` helper uses (`src/mailflow/core/identity.py`) and match it in the assertion; the implementation reuses `key` directly, so the test is asserting the helper's actual format.

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_pipeline_dlq_durable.py -v`
Expected: FAIL — `Pipeline.__init__() got an unexpected keyword argument 'dlq_store'`.

- [ ] **Step 3a: Imports + constructor**

In `src/mailflow/core/pipeline.py`, extend the stdlib imports (after line 10) and add `DeadLetterStore` / `DeadLetterRecord`:

```python
from __future__ import annotations

import base64
from datetime import datetime, timezone

from pydantic import BaseModel
```

Add `DeadLetterRecord` to the observability import (line 26):

```python
from mailflow.core.observability import DeadLetter, DeadLetterRecord, DecisionTrace, RunReport
```

Add `DeadLetterStore` to the ports import list (Task 2's import block):

```python
    CursorStore,
    DeadLetterStore,
    DedupeStore,
```

Add the constructor parameter (after `auth_refresher: AuthRefresher | None = None,` from Task 2) and store it:

```python
        auth_refresher: AuthRefresher | None = None,
        dlq_store: DeadLetterStore | None = None,
    ) -> None:
```

```python
        self.auth_refresher = auth_refresher
        self.dlq_store = dlq_store
```

- [ ] **Step 3b: Thread attempts + error_class into the size-guard DLQ calls**

In `_process`, update the two size-guard `_dead_letter` calls (lines 117-126) to carry the attempt count and an error class:

```python
        size = self.provider.message_size(msg) or msg.size_bytes
        if size <= 0:
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"size unknown (fail-closed): {size}",
                attempts=attempts, error_class="SizeUnknownError",
            )
        if size > self.config.max_message_bytes:
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"oversized: {size} > {self.config.max_message_bytes}",
                attempts=attempts, error_class="OversizedMessageError",
            )
```

In the `except PermanentError` branch (Task 2's body) add `attempts`/`error_class`:

```python
        except PermanentError as exc:
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"{type(exc).__name__}: {exc}",
                attempts=attempts, error_class=type(exc).__name__,
            )
```

In `_retry_or_dead_letter` (lines 183-186) add them to the terminal DLQ call:

```python
        if attempts >= self.config.max_attempts:
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"{type(exc).__name__}: {exc}",
                attempts=attempts, error_class=type(exc).__name__,
            )
```

In `_handle_auth_error` (Task 2) add them to the DLQ call:

```python
        except Exception as retry_exc:  # noqa: BLE001 - one shot only; any failure -> DLQ
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"auth retry failed after refresh: "
                       f"{type(retry_exc).__name__}: {retry_exc}",
                attempts=attempts, error_class=type(exc).__name__,
            )
```

- [ ] **Step 3c: `_dead_letter` writes the durable record**

Replace `_dead_letter` (lines 212-229) and add `_dead_letter_record`:

```python
    def _dead_letter(
        self, canonical_id: str, msg: RawMessage, key: str, report: RunReport, *,
        reason: str, attempts: int = 0, error_class: str = "",
    ) -> Disposition:
        self.dlq_emitter.emit(  # DLQ is just another Emitter sink in the core spine
            EmailEvent(
                schema_version=SCHEMA_VERSION,
                tenant=self.config.tenant,
                ordering_key=msg.stream.mailbox,
                email=self._stub_email(canonical_id, msg),
            )
        )
        self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
        # FROZEN CONTRACT: add_dead_letter() owns the count; record() does not. Keep these
        # two as a paired unit -> exactly one count per poison message.
        report.add_dead_letter(
            DeadLetter(canonical_id=canonical_id, reason=reason,
                       provider_message_id=msg.provider_message_id)
        )
        report.record(self._trace(canonical_id, msg, Disposition.dead_lettered, "dlq", reason=reason))
        # ADDITIVE: durable, replayable record. Does NOT touch any counter.
        if self.dlq_store is not None:
            self.dlq_store.put(
                self._dead_letter_record(
                    canonical_id, msg, key,
                    reason=reason, attempts=attempts, error_class=error_class,
                )
            )
        return Disposition.dead_lettered

    def _dead_letter_record(
        self, canonical_id: str, msg: RawMessage, key: str, *,
        reason: str, attempts: int, error_class: str,
    ) -> DeadLetterRecord:
        return DeadLetterRecord(
            record_id=key,  # = idempotency_key; re-dead-letter overwrites
            tenant=self.config.tenant,
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            mailbox=msg.stream.mailbox,
            folder=msg.stream.folder,
            canonical_id=canonical_id,
            reason=reason,
            error_class=error_class,
            attempts=attempts,
            size_bytes=msg.size_bytes,
            thread_key=msg.thread_key,
            cursor_value=msg.cursor.value,
            cursor_order=msg.cursor.order,
            received_at=msg.received_at,
            dead_lettered_at=datetime.now(timezone.utc),
            raw_b64=base64.b64encode(msg.raw_bytes).decode("ascii") if msg.raw_bytes else "",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_pipeline_dlq_durable.py tests/test_pipeline_routing.py tests/test_pipeline_invariants.py tests/test_pipeline_size_failclosed.py -v`
Expected: PASS (durable write works; the count-once invariant and size-guard tests stay green because the durable write is additive).

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/core/pipeline.py tests/test_pipeline_dlq_durable.py
git commit -m "feat(dlq): write durable DeadLetterRecord alongside the count-once DLQ path

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `redrive()` entrypoint — re-submit dead-lettered messages

**Files:**
- Create: `src/mailflow/core/redrive.py`
- Test: `tests/test_redrive.py` (create)

**Interfaces:**
- Consumes: `DeadLetterStore` (Task 4), `DeadLetterRecord` (Task 4), `Pipeline`/`PipelineConfig`, `MimeEnvelopeParser`, `MimeExtractor`, `FilterChain`, in-memory stores.
- Produces:
  - `rebuild_raw_message(record: DeadLetterRecord) -> RawMessage`
  - `RedriveReport(BaseModel)` with `examined`, `resubmitted`, `still_dead_lettered`, `run: RunReport`
  - `redrive(*, store, emitter, dlq_emitter, blob_store, config, parser=None, extractor=None, filters=None, cleaner=None, classifier=None, limit=None) -> RedriveReport`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_redrive.py`:

```python
"""redrive() rebuilds each durable dead-letter into its original RawMessage and re-runs
it through a fresh, redrive-scoped pipeline. A record that now reaches a non-DLQ terminal
disposition is deleted from the store; one that fails again is kept."""

from __future__ import annotations

from typing import Any

from mailflow.core.errors import PermanentError
from mailflow.core.models import Envelope, RawMessage, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.redrive import rebuild_raw_message, redrive
from mailflow.core.observability import DeadLetterRecord
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDeadLetterStore,
    InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def raw(mid: str = "m1") -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: a@partner.com\r\n"
            f"To: ops@acme.com\r\nSubject: hi\r\n\r\nbody").encode()


class RaisingExtractor:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def extract(self, msg: RawMessage, env: Envelope) -> Any:
        raise self.exc


def _poison_run_into(dlq_store: InMemoryDeadLetterStore, exc: Exception) -> None:
    Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(), filters=FilterChain([]),
        extractor=RaisingExtractor(exc),
        emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="acme"),
        dlq_store=dlq_store,
    ).run_once()


def test_rebuild_raw_message_restores_bytes_and_stream() -> None:
    dlq_store = InMemoryDeadLetterStore()
    _poison_run_into(dlq_store, PermanentError("synthetic"))
    rec = dlq_store.list_pending()[0]
    msg = rebuild_raw_message(rec)
    assert msg.raw_bytes == raw("m1")
    assert msg.stream == STREAM
    assert msg.provider_message_id == "m1"


def test_redrive_resubmits_and_clears_on_success() -> None:
    # The raw is valid RFC822; the original failure was a synthetic extractor error.
    dlq_store = InMemoryDeadLetterStore()
    _poison_run_into(dlq_store, PermanentError("synthetic"))
    assert len(dlq_store.list_pending()) == 1

    out_emit, out_dlq = MemoryEmitter(), MemoryEmitter()
    report = redrive(
        store=dlq_store, emitter=out_emit, dlq_emitter=out_dlq,
        blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="acme"),
    )
    assert report.examined == 1 and report.resubmitted == 1
    assert report.still_dead_lettered == 0
    assert len(out_emit.events) == 1            # re-emitted with a real MimeExtractor
    assert dlq_store.list_pending() == []       # cleared after success


def test_redrive_keeps_record_when_it_fails_again() -> None:
    dlq_store = InMemoryDeadLetterStore()
    _poison_run_into(dlq_store, PermanentError("synthetic"))
    report = redrive(
        store=dlq_store, emitter=MemoryEmitter(), dlq_emitter=MemoryEmitter(),
        blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="acme"),
        extractor=RaisingExtractor(PermanentError("still broken")),
    )
    assert report.examined == 1 and report.resubmitted == 0
    assert report.still_dead_lettered == 1
    assert len(dlq_store.list_pending()) == 1


def test_redrive_empty_store_is_a_noop() -> None:
    report = redrive(
        store=InMemoryDeadLetterStore(), emitter=MemoryEmitter(),
        dlq_emitter=MemoryEmitter(), blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )
    assert report.examined == 0 and report.resubmitted == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_redrive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mailflow.core.redrive'`.

- [ ] **Step 3: Write the implementation**

Create `src/mailflow/core/redrive.py`:

```python
"""Redrive — re-submit durable dead-letters through the pipeline once the root cause is
fixed (A2 recovery). Each record is rebuilt into its original RawMessage and run through a
FRESH, redrive-scoped Pipeline: a fresh DedupeStore (so the original marked-done claim
does not reject the replay) and a throwaway CursorStore, but the operator's REAL emitter /
dlq_emitter / blob_store. A record that reaches a non-DLQ terminal disposition is deleted
from the store; one that dead-letters again is kept for a later attempt.

At-least-once is preserved: the re-emitted event carries the original idempotency_key, so a
downstream consumer dedupes any message that was already partially delivered (A4)."""

from __future__ import annotations

import base64
from typing import Iterable, Iterator

from pydantic import BaseModel, Field

from mailflow.core.models import Cursor, RawMessage, StreamRef
from mailflow.core.observability import DeadLetterRecord, RunReport
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.ports import (
    BlobStore,
    Classifier,
    ContentCleaner,
    ContentExtractor,
    DeadLetterStore,
    Emitter,
    EnvelopeParser,
)
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.stores.memory import InMemoryCursorStore, InMemoryDedupeStore


class RedriveReport(BaseModel):
    examined: int = 0
    resubmitted: int = 0          # reached a non-dead-letter terminal disposition
    still_dead_lettered: int = 0
    run: RunReport = Field(default_factory=RunReport)


def rebuild_raw_message(record: DeadLetterRecord) -> RawMessage:
    return RawMessage(
        provider=record.provider,
        provider_message_id=record.provider_message_id,
        stream=StreamRef(mailbox=record.mailbox, folder=record.folder),
        size_bytes=record.size_bytes,
        received_at=record.received_at,
        cursor=Cursor(value=record.cursor_value, order=record.cursor_order),
        raw_bytes=base64.b64decode(record.raw_b64) if record.raw_b64 else b"",
        thread_key=record.thread_key,
    )


class _RedriveProvider:
    """A one-shot MailboxProvider that re-yields exactly the rebuilt RawMessages
    (preserving cursor + thread_key, which MemoryProvider/SeedEmail would lose)."""

    PROVIDER = "redrive"

    def __init__(self, messages: list[RawMessage]) -> None:
        self._by_stream: dict[StreamRef, list[RawMessage]] = {}
        for msg in messages:
            self._by_stream.setdefault(msg.stream, []).append(msg)

    def connect(self) -> None:
        return None

    def sync_streams(self) -> Iterable[StreamRef]:
        return list(self._by_stream)

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        for msg in self._by_stream.get(stream, []):
            yield msg

    def message_size(self, msg: RawMessage) -> int | None:
        return msg.size_bytes


def _accumulate(into: RunReport, part: RunReport) -> None:
    into.fetched += part.fetched
    into.emitted += part.emitted
    into.dropped += part.dropped
    into.duplicates += part.duplicates
    into.dead_lettered += part.dead_lettered
    into.traces.extend(part.traces)
    into.dlq.extend(part.dlq)


def redrive(
    *,
    store: DeadLetterStore,
    emitter: Emitter,
    dlq_emitter: Emitter,
    blob_store: BlobStore,
    config: PipelineConfig,
    parser: EnvelopeParser | None = None,
    extractor: ContentExtractor | MimeExtractor | None = None,
    filters: FilterChain | None = None,
    cleaner: ContentCleaner | None = None,
    classifier: Classifier | None = None,
    limit: int | None = None,
) -> RedriveReport:
    parser = parser if parser is not None else MimeEnvelopeParser()
    extractor = extractor if extractor is not None else MimeExtractor()
    filters = filters if filters is not None else FilterChain([])

    report = RedriveReport()
    for record in store.list_pending(limit=limit):
        report.examined += 1
        msg = rebuild_raw_message(record)
        pipeline = Pipeline(
            provider=_RedriveProvider([msg]),
            parser=parser,
            filters=filters,
            extractor=extractor,
            emitter=emitter,
            dlq_emitter=dlq_emitter,
            cursor_store=InMemoryCursorStore(),   # throwaway: redrive does not own cursors
            dedupe_store=InMemoryDedupeStore(),    # fresh: bypass the original marked-done claim
            blob_store=blob_store,
            config=config,
            classifier=classifier,
            cleaner=cleaner,
        )
        run = pipeline.run_once()
        _accumulate(report.run, run)
        if run.fetched >= 1 and run.dead_lettered == 0:
            store.delete(record.record_id)
            report.resubmitted += 1
        else:
            report.still_dead_lettered += 1
    return report
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_redrive.py -v`
Expected: PASS.

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/core/redrive.py tests/test_redrive.py
git commit -m "feat(dlq): redrive entrypoint re-submits dead-letters through a fresh pipeline

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Wire `dlq_store` + `auth_refresher` through the builder, registry, and Gmail composition

**Files:**
- Modify: `src/mailflow/registry.py:31-38` (add `DEADLETTER_KINDS`), `:10-30` (imports), end of file (`build_dead_letter_store`)
- Modify: `src/mailflow/builder.py:26-72` (pass `dlq_store` + `auth_refresher` overrides)
- Modify: `src/mailflow/adapters/gmail/composition.py:28-68` (accept `dlq_store`, wire `auth_refresher=token_provider`)
- Modify: `src/mailflow/adapters/gmail/live.py:140-190` (thread `dlq_store` through `run_service`)
- Test: `tests/test_overrides_wiring.py` (append), `tests/test_registry.py` (append), `tests/test_gmail_composition_cleaner.py` (append a wiring assertion) or new `tests/test_gmail_failure_wiring.py`

**Interfaces:**
- Consumes: `InMemoryDeadLetterStore`/`SqliteDeadLetterStore` (Task 5), `Pipeline(auth_refresher=, dlq_store=)` (Tasks 2/6), `OAuthTokenProvider.force_refresh` (Task 1).
- Produces: `build_dead_letter_store(kind: str, params: dict[str, Any]) -> DeadLetterStore`; `build_from_config(..., overrides={"dlq_store": ..., "auth_refresher": ...})`; `build_gmail_runtime(..., dlq_store=None)` wiring `auth_refresher=token_provider`; `run_service(..., dlq_store=None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registry.py`:

```python
from mailflow.registry import build_dead_letter_store
from mailflow.stores.memory import InMemoryDeadLetterStore
from mailflow.stores.sqlite import SqliteDeadLetterStore


def test_build_dead_letter_store_memory() -> None:
    assert isinstance(build_dead_letter_store("memory", {}), InMemoryDeadLetterStore)


def test_build_dead_letter_store_sqlite(tmp_path) -> None:
    store = build_dead_letter_store("sqlite", {"path": str(tmp_path / "dlq.db")})
    assert isinstance(store, SqliteDeadLetterStore)
```

Append to `tests/test_overrides_wiring.py` (match the file's existing `build_from_config` + `MailflowConfig` usage; mirror how it builds a minimal config):

```python
def test_overrides_inject_dlq_store_and_auth_refresher() -> None:
    from mailflow.stores.memory import InMemoryDeadLetterStore

    class _Refresher:
        def force_refresh(self) -> None:
            return None

    dlq_store = InMemoryDeadLetterStore()
    refresher = _Refresher()
    pipe = build_from_config(
        _minimal_config(),  # the helper already used in this test module
        overrides={"dlq_store": dlq_store, "auth_refresher": refresher},
    )
    assert pipe.dlq_store is dlq_store
    assert pipe.auth_refresher is refresher
```

> If `tests/test_overrides_wiring.py` has no `_minimal_config()` helper, reuse whatever config-construction pattern the file already uses for its existing override tests (read the file first); the only new assertions are `pipe.dlq_store is dlq_store` and `pipe.auth_refresher is refresher`.

Create `tests/test_gmail_failure_wiring.py`:

```python
"""The Gmail composition root wires the token provider as the pipeline's auth_refresher
and accepts a dlq_store, so the live path gets durable DLQ + refresh-once for free."""

from __future__ import annotations

import inspect

from mailflow.adapters.gmail.composition import build_gmail_runtime
from mailflow.adapters.gmail.live import run_service


def test_build_gmail_runtime_accepts_dlq_store() -> None:
    assert "dlq_store" in inspect.signature(build_gmail_runtime).parameters


def test_run_service_accepts_dlq_store() -> None:
    assert "dlq_store" in inspect.signature(run_service).parameters
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_registry.py::test_build_dead_letter_store_memory tests/test_gmail_failure_wiring.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_dead_letter_store'` and missing `dlq_store` parameter.

- [ ] **Step 3a: Registry helper**

In `src/mailflow/registry.py`, add the imports (extend the `stores` imports, lines 24-30) and a kinds set + builder:

```python
from mailflow.core.ports import (
    BlobStore,
    CursorStore,
    DeadLetterStore,
    DedupeStore,
    Emitter,
    Filter,
)
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDeadLetterStore,
    InMemoryDedupeStore,
)
from mailflow.stores.sqlite import (
    SqliteCursorStore,
    SqliteDeadLetterStore,
    SqliteDedupeStore,
)
```

```python
DEADLETTER_KINDS = {"memory", "sqlite"}


def build_dead_letter_store(kind: str, params: dict[str, Any]) -> DeadLetterStore:
    if kind == "memory":
        return InMemoryDeadLetterStore()
    if kind == "sqlite":
        return SqliteDeadLetterStore(str(params["path"]))
    raise ValueError(f"unknown dead-letter store kind {kind!r}")
```

- [ ] **Step 3b: Builder overrides**

In `src/mailflow/builder.py`, replace the `dlq_emitter` + `Pipeline(...)` construction (lines 56-72) so the override-supplied `dlq_store`/`auth_refresher` reach the pipeline:

```python
    dlq_store = ov["dlq_store"] if "dlq_store" in ov else None
    auth_refresher = ov["auth_refresher"] if "auth_refresher" in ov else None
    return Pipeline(
        provider=provider,
        parser=MimeEnvelopeParser(),
        filters=filters,
        extractor=MimeExtractor(),
        emitter=emitter,
        dlq_emitter=build_emitter("memory"),
        cursor_store=cursor_store,
        dedupe_store=dedupe_store,
        blob_store=blob_store,
        cleaner=ov["cleaner"] if "cleaner" in ov else ThinContentCleaner(),
        config=PipelineConfig(
            tenant=cfg.tenant,
            max_message_bytes=cfg.max_message_bytes,
            max_attempts=cfg.max_attempts,
        ),
        auth_refresher=auth_refresher,
        dlq_store=dlq_store,
    )
```

- [ ] **Step 3c: Gmail composition**

In `src/mailflow/adapters/gmail/composition.py`, add `dlq_store` to `build_gmail_runtime` (after `cleaner` at line 42) and pass it + `auth_refresher=token_provider` into the `Pipeline`:

```python
    cleaner: ContentCleaner | None = None,
    dlq_store: DeadLetterStore | None = None,
) -> GmailPubSubRuntime:
```

Add the import:

```python
from mailflow.core.ports import (
    BlobStore,
    Classifier,
    ContentCleaner,
    CursorStore,
    DeadLetterStore,
    DedupeStore,
    Emitter,
    Filter,
)
```

In the `Pipeline(...)` call (lines 54-66), append:

```python
        config=PipelineConfig(tenant=tenant, max_attempts=gmail_cfg.max_attempts),
        classifier=classifier,
        cleaner=cleaner,
        auth_refresher=token_provider,   # A2: refresh-once uses the live OAuth provider
        dlq_store=dlq_store,
    )
```

> `token_provider` is typed `TokenProvider` in this signature; `Pipeline.auth_refresher` expects `AuthRefresher`. The concrete `OAuthTokenProvider` satisfies both, but mypy checks the *declared* `TokenProvider` type, which lacks `force_refresh`. Fix by widening the composition parameter annotation to `TokenProvider | AuthRefresher` and adding `AuthRefresher` to the ports import; or (simpler) annotate the parameter as `RefreshableTokenProvider` (Task 3) since the live provider satisfies it. Use `RefreshableTokenProvider` from `mailflow.adapters.gmail.transport` for the `token_provider` parameter type so both `GmailClient` and the pipeline `auth_refresher` type-check.

- [ ] **Step 3d: `run_service` threads `dlq_store`**

In `src/mailflow/adapters/gmail/live.py`, add `dlq_store: DeadLetterStore | None = None` to `run_service` (after `cleaner` at line 155), import `DeadLetterStore` in the ports import (lines 23-32), and forward it into `build_gmail_runtime` (line 184-190):

```python
    runtime = build_gmail_runtime(
        gmail_cfg=gmail_cfg, pubsub_cfg=pubsub_cfg, tenant=tenant,
        token_provider=token_provider, transport=transport,
        emitter=emitter, dlq_emitter=dlq_emitter,
        cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
        filters=filters, cleaner=cleaner, dlq_store=dlq_store,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_registry.py tests/test_overrides_wiring.py tests/test_gmail_failure_wiring.py tests/test_gmail_e2e.py -v`
Expected: PASS.

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/registry.py src/mailflow/builder.py src/mailflow/adapters/gmail/composition.py src/mailflow/adapters/gmail/live.py tests/test_registry.py tests/test_overrides_wiring.py tests/test_gmail_failure_wiring.py
git commit -m "feat(dlq+auth): wire dlq_store + auth_refresher through builder, registry, gmail

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Documented redrive runbook + full-suite green

**Files:**
- Create: `docs/dlq-redrive.md`
- Test: full suite + mypy (no new test file; this task documents and verifies the whole feature set)

**Interfaces:**
- Consumes: everything from Tasks 1-8.
- Produces: the operator-facing redrive procedure (explicitly requested by the spec).

- [ ] **Step 1: Write the runbook**

Create `docs/dlq-redrive.md`:

````markdown
# DLQ & Redrive Runbook

mailflow dead-letters a message when it can never succeed as-is (poison/oversized/invalid
base64 → `PermanentError`), when a bounded retry budget is exhausted (`TransientError`
after `max_attempts`), or when a 401 still fails after one forced refresh + one retry. Every
dead-letter does two things:

1. **Counts once** in the `RunReport` (`add_dead_letter()` — the frozen single-count path).
2. **Persists a durable, replayable `DeadLetterRecord`** in the wired `DeadLetterStore`
   (when one is configured), carrying the original raw RFC822 bytes (base64), provider ids,
   stream, failure reason + error class, attempt count, and the cursor it was at.

The cursor always advances past a dead-lettered message, so the stream is never blocked.

## Wiring a durable store

Core / zero-setup path (`build_from_config`):

```python
from mailflow.builder import build_from_config
from mailflow.stores.sqlite import SqliteDeadLetterStore

pipeline = build_from_config(cfg, overrides={"dlq_store": SqliteDeadLetterStore("mailflow.db")})
```

Live Gmail path (`run_service`):

```python
run_service(..., dlq_store=SqliteDeadLetterStore("mailflow.db"))
```

Without a `dlq_store`, dead-letters still count and still hit the `dlq_emitter` sink, but
they are NOT replayable — wire a store if you want redrive.

## Inspecting the DLQ

```python
from mailflow.stores.sqlite import SqliteDeadLetterStore

for rec in SqliteDeadLetterStore("mailflow.db").list_pending():
    print(rec.record_id, rec.error_class, rec.attempts, rec.reason)
```

Or directly: `sqlite3 mailflow.db "SELECT record_id, json_extract(payload,'$.error_class'),
json_extract(payload,'$.reason') FROM dead_letters;"`

## Redrive procedure

**Fix the root cause first.** Redrive replays the exact original bytes; if the underlying
problem (bad credentials, a parser bug, a too-low size ceiling, a downstream outage) is not
resolved, the message will simply dead-letter again and stay in the store.

1. Identify the failure class from `error_class` / `reason`.
2. Remediate:
   - `OversizedMessageError` → raise `max_message_bytes` in config.
   - `SizeUnknownError` → fix the provider's size metadata.
   - `PermanentError` (invalid base64 / 403 / 404 / 410) → usually NOT redrivable; the
     message is genuinely poison. Redrive only after confirming the cause was transient
     tooling (e.g. a since-fixed extractor bug).
   - `AuthError` (persisted after refresh+retry) / `TransientError` → fix credentials or
     wait out the downstream outage, then redrive.
3. Run the redrive:

```python
from mailflow.core.pipeline import PipelineConfig
from mailflow.core.redrive import redrive
from mailflow.emit.pubsub import build_pubsub_emitter   # or your real emitter
from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.sqlite import SqliteDeadLetterStore

store = SqliteDeadLetterStore("mailflow.db")
report = redrive(
    store=store,
    emitter=build_pubsub_emitter(project_id="...", topic="..."),  # your PRODUCTION emitter
    dlq_emitter=...,           # where re-failures go
    blob_store=LocalBlobStore("attachments"),
    config=PipelineConfig(tenant="acme"),
    limit=None,                # or an int to batch
)
print(report.examined, report.resubmitted, report.still_dead_lettered)
```

`redrive()` rebuilds each record into its original `RawMessage` and runs it through a
**fresh, redrive-scoped pipeline** (a fresh `DedupeStore` so the original marked-done claim
does not reject the replay, and a throwaway cursor store) using your **real** emitter. A
record that now reaches a non-DLQ terminal disposition (emitted/dropped/duplicate) is
**deleted** from the store; one that fails again is **kept** for a later attempt.

**At-least-once:** the re-emitted event carries the original `idempotency_key`
`(tenant, mailbox, provider_message_id)`, so any consumer that already saw a partially
delivered copy dedupes it (§A4). Redrive is safe to re-run.

## Auth failure flow (401 / 403)

- **401 → `AuthError`:** the pipeline forces exactly ONE token refresh
  (`AuthRefresher.force_refresh()`), retries the message exactly once, and dead-letters if
  it still fails — it never loops on `max_attempts`. On the Gmail fetch surface the same
  refresh-once is applied inside `GmailClient` (one refresh + one retry per request).
- **403 → `PermanentError`:** dead-letters immediately, no retry (a 403 is a durable
  permission problem; redrive only after the grant/scope is fixed).
````

- [ ] **Step 2: Run the full suite + types**

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest`
Expected: PASS (all pre-existing tests plus the new failure-handling tests).

Run: `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
Expected: `Success: no issues found`.

- [ ] **Step 3: Commit**

```bash
git add docs/dlq-redrive.md
git commit -m "docs(dlq): operator runbook for the DLQ + redrive path and auth flow

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- *Replayable DLQ + documented redrive path* → durable record (Task 4), stores (Task 5), pipeline write (Task 6), `redrive()` entrypoint (Task 7), wiring (Task 8), runbook (Task 9). ✅ The original raw message ref (`raw_b64`), provider ids, failure reason (`reason`/`error_class`), and attempt count are all on `DeadLetterRecord`. ✅
- *401→refresh→retry-once / 403→permanent* → pipeline refresh-once routing (Task 2), Gmail provider/token `force_refresh` (Task 1), Gmail fetch-surface 401 handling (Task 3), errors.py docstring corrected (Task 2). 403 stays immediate-DLQ via the unchanged `PermanentError` branch. ✅

**2. Placeholder scan:** No TBD/TODO/"handle errors appropriately". Every code step shows complete code. The one judgement call (the exact `idempotency_key` separator in Task 6's `record_id` assertion) is flagged with the file to confirm against (`core/identity.py`) rather than guessed — the implementation reuses `key` verbatim so the assertion just needs to match the real helper output. ✅

**3. Type consistency:**
- `force_refresh(self) -> None` is identical across `OAuthTokenProvider` (Task 1), `AuthRefresher` (Task 2), and `RefreshableTokenProvider` (Task 3). ✅
- `DeadLetterStore` methods (`put`/`list_pending(*, limit)`/`delete`) match across the port (Task 4), both adapters (Task 5), redrive (Task 7), and registry (Task 8). ✅
- `_dead_letter(..., *, reason, attempts=0, error_class="")` signature matches all six call sites updated in Task 6 (two size guards, Permanent branch, transient terminal, auth retry-fail). ✅
- `Pipeline(..., auth_refresher=None, dlq_store=None)` keyword names match the builder, composition, and all test `build()` helpers. ✅
- `redrive(...)` defaults (`MimeEnvelopeParser`, `MimeExtractor`, `FilterChain([])`) match the core builder's defaults, so the runbook's minimal call works. ✅

**4. Frozen-contract guards:** Count-once preserved (durable write is additive, after the `add_dead_letter`/`record` pair; Task 6 test asserts `dead_lettered == 1`). Cursor monotonicity preserved (auth refresh-once returns a terminal `Disposition` on DLQ or `emitted` on success — exactly one terminal per message; Task 2 test asserts the cursor advanced on auth-DLQ and the non-terminal transient path is unchanged). Dedupe shape untouched (redrive uses a fresh store rather than adding a port method). ✅

**5. Importability / commit ordering:** `AuthRefresher` and `DeadLetterStore` ports are added in the same commit as the modules that import them; `observability` (events→models only) gains no cycle from `ports` importing `DeadLetterRecord`; `core/redrive.py` is leaf (imported by nobody in the core path). Each task leaves the tree importable and mypy-clean. ✅

**Fix applied during review:** Task 8 Step 3c originally passed `token_provider` (declared `TokenProvider`, which lacks `force_refresh`) as `auth_refresher` — mypy-strict would reject it. Resolved by retyping the composition's `token_provider` parameter to `RefreshableTokenProvider` (Task 3), which the live `OAuthTokenProvider` satisfies and which carries `force_refresh`.
