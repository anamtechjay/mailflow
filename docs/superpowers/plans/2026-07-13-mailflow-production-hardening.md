# mailflow Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the production-readiness gaps found in the 2026-07-13 review — unbounded
dedupe-table growth, unhelpful config errors, missing durable dead-letter storage/redrive
tooling, Postgres hardening, and minimal CI — without touching Service Bus or Event Hub
configuration/provisioning (explicitly out of scope per the user).

**Architecture:** Every change is additive to the existing port/registry/facade seams —
no `core/pipeline.py` behavior changes, no new abstractions beyond what each gap needs.
`DedupeStore.mark_done`/`purge_expired` gets a real expiry column (3 stores); `connect()`
gains a durable `DeadLetterStore` for `memory`+`gmail` (Graph Event Hubs/Service Bus
deferred — see Task 7 note); a new `mailflow redrive`/`mailflow purge` CLI surfaces
`core/redrive.py` (already built, unused) and the new purge method; DB store constructors
get a small retry-with-backoff wrapper; Postgres gains secret-ref DSN resolution.

**Tech Stack:** Python 3.12, pydantic 2.6+, pytest 8+, mypy strict, psycopg 3 (postgres
extra, already added). No new runtime dependencies beyond what's already in `pyproject.toml`.

## Global Constraints

- **TDD, non-negotiable** (CLAUDE.md): failing test → confirm it fails for the stated
  reason → minimal implementation → green → `mypy` clean → one behavior per commit.
- **`python -m pytest` / `python -m mypy`** — this Windows checkout has no `.venv`; use
  plain `python` (already confirmed working throughout this session).
- **Never `git push`.** Commits only, local.
- **Every commit leaves the tree importable** — mypy strict scopes to `packages =
  ["mailflow"]`; if module A imports B, commit B first.
- **Out of scope, explicitly:** Azure Service Bus and Event Hub configuration/provisioning
  — do not touch `src/mailflow/adapters/servicebus/**`, and do not add `dlq_store` to
  `src/mailflow/adapters/graph/composition.py`/`live.py` (Graph's Event Hubs path) in this
  plan. Task 7 wires durable DLQ for `memory` + `gmail` only; Graph/Service Bus DLQ wiring
  is called out as a deferred follow-up, not attempted here.
- **License/CHANGELOG/Dockerfile/dist rebuild are deliberately excluded** per the user's
  "focus on codebase/functionality, not packaging paperwork" instruction — only `py.typed`
  and dependency version bounds are addressed (Phase 4), plus a minimal CI workflow.

---

## Phase 1 — Critical correctness

### Task 1: `ConfigError` instead of raw `KeyError` on missing Gmail/Graph credentials

**Files:**
- Modify: `src/mailflow/facade.py` (add `_require` helper after imports ~line 60; replace
  raw `credentials["..."]` indexing at lines 374-376, 428-436, 459-461)
- Test: `tests/test_facade_credentials_errors.py` (create)

**Interfaces:**
- Produces: `_require(credentials: dict[str, Any], key: str, *, provider: str) -> str` —
  used only inside `facade.py`, not part of the public API.

- [ ] **Step 1: Write the failing tests.** Create `tests/test_facade_credentials_errors.py`:

```python
"""connect() must raise a clear ConfigError (not a raw KeyError) when a required
credential is missing — the first thing an operator hits when misconfiguring a deploy."""

from __future__ import annotations

import pytest

from mailflow import connect
from mailflow.core.errors import ConfigError


def test_connect_gmail_missing_client_id_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="client_id"):
        connect(
            "gmail",
            credentials={
                "client_secret_ref": "env://X",
                "oauth_refresh_token_ref": "env://Y",
                "project_id": "p", "topic": "t", "subscription": "s",
            },
            mailbox="me",
        )


def test_connect_gmail_missing_project_id_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="project_id"):
        connect(
            "gmail",
            credentials={
                "client_id": "c", "client_secret_ref": "env://X",
                "oauth_refresh_token_ref": "env://Y",
                "topic": "t", "subscription": "s",
            },
            mailbox="me",
        )


def test_connect_graph_missing_tenant_id_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="tenant_id"):
        connect(
            "graph",
            credentials={
                "client_id": "c", "client_secret_ref": "env://X",
                "namespace": "n", "hub": "h", "tenant_domain": "d",
            },
            mailbox="me@acme.com",
        )
```

- [ ] **Step 2: Run to verify RED.** Run:
  `python -m pytest tests/test_facade_credentials_errors.py -v`
  Expected: all 3 FAIL with `KeyError: 'client_id'` / `KeyError: 'project_id'` /
  `KeyError: 'tenant_id'` (not `ConfigError`) — confirms today's actual (unhelpful) behavior.

- [ ] **Step 3: Add the `_require` helper and `ConfigError` import.** In
  `src/mailflow/facade.py`, add to the existing import block (after the
  `mailflow.config.state` import, ~line 27):

```python
from mailflow.core.errors import ConfigError
```

  Then add this function immediately after `normalize_filters` (which ends at line 112,
  right before `class Mailflow:`):

```python
def _require(credentials: dict[str, Any], key: str, *, provider: str) -> str:
    """Fetch a required credential, raising a clear ConfigError (not a raw KeyError)
    when it's missing or blank. Kept provider-agnostic so gmail/graph builders share it."""
    value = credentials.get(key)
    if not value:
        raise ConfigError(f"{provider} credentials missing required key {key!r}")
    return str(value)
```

- [ ] **Step 4: Replace the raw indexing sites.** In `src/mailflow/facade.py`:

  In `_build_gmail_fetcher` (currently lines 374-376):
```python
    cfg = GmailConfig(
        client_id=_require(credentials, "client_id", provider="gmail"),
        client_secret_ref=_require(credentials, "client_secret_ref", provider="gmail"),
        oauth_refresh_token_ref=_require(credentials, "oauth_refresh_token_ref", provider="gmail"),
        mailboxes=[mbx],
    )
```

  In `_build_gmail_live` (currently lines 428-436):
```python
    gmail_cfg = GmailConfig(
        client_id=_require(credentials, "client_id", provider="gmail"),
        client_secret_ref=_require(credentials, "client_secret_ref", provider="gmail"),
        oauth_refresh_token_ref=_require(credentials, "oauth_refresh_token_ref", provider="gmail"),
        mailboxes=mailboxes,
    )
    pubsub_cfg = PubSubConfig(
        project_id=_require(credentials, "project_id", provider="gmail"),
        topic=_require(credentials, "topic", provider="gmail"),
        subscription=_require(credentials, "subscription", provider="gmail"),
    )
```

  In `_graph_config` (currently lines 459-461) — this is the shared Graph app-credential
  builder used by both Event Hubs and Service Bus delivery; it is NOT eventhub/servicebus
  *configuration* itself (that's `_graph_configs`'s `EventHubConfig(namespace=...)` and
  `_servicebus_configs`'s `ServiceBusConfig(...)`, both left untouched per the global
  constraint above):
```python
    return GraphConfig(
        tenant_id=_require(credentials, "tenant_id", provider="graph"),
        client_id=_require(credentials, "client_id", provider="graph"),
        client_secret_ref=_require(credentials, "client_secret_ref", provider="graph"),
        mailboxes=mailboxes,
    )
```

- [ ] **Step 5: Run to verify GREEN.** Run:
  `python -m pytest tests/test_facade_credentials_errors.py -v`
  Expected: all 3 PASS. Then run the full fast suite to confirm no regression:
  `python -m pytest -m "not slow" -q` — expected: same pass count as before plus 3.
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 6: Commit.**

```bash
git add src/mailflow/facade.py tests/test_facade_credentials_errors.py
git commit -m "fix(facade): raise ConfigError instead of KeyError on missing gmail/graph credentials"
```

---

### Task 2: TTL purge — `InMemoryDedupeStore`

**Files:**
- Modify: `src/mailflow/stores/memory.py`
- Test: `tests/test_memory_stores_purge.py` (create)

**Interfaces:**
- Produces: `InMemoryDedupeStore.purge_expired(self, *, now: float | None = None) -> int`
  — NOT added to the `DedupeStore` Protocol (`core/ports.py` unchanged) — it's a concrete
  housekeeping method callers reach via the concrete store object, exactly like
  `health()` already does for store-specific extras. This avoids breaking any existing
  `DedupeStore` implementation (fakes, other adapters) that doesn't have it.

- [ ] **Step 1: Write the failing test.** Create `tests/test_memory_stores_purge.py`:

```python
"""purge_expired(): mark_done(key, ttl_seconds) must actually expire -- until this test,
ttl_seconds was accepted and silently discarded, so claims accumulated forever."""

from __future__ import annotations

from mailflow.stores.memory import InMemoryDedupeStore


def test_purge_expired_removes_only_expired_done_claims() -> None:
    clock = {"now": 1_000.0}
    store = InMemoryDedupeStore()
    store.try_claim("expired", 300)
    store.mark_done("expired", ttl_seconds=60)   # expires at t=1060
    store.try_claim("fresh", 300)
    store.mark_done("fresh", ttl_seconds=600)    # expires at t=1600
    store.try_claim("not-done", 300)             # never marked done -- must survive

    removed = store.purge_expired(now=1_100.0)   # past "expired"'s ttl, before "fresh"'s

    assert removed == 1
    assert store.try_claim("expired", 300) is True   # gone -> claimable again
    assert store.try_claim("fresh", 300) is False     # still done, not yet expired
    assert store.try_claim("not-done", 300) is False  # untouched, still claimed
```

- [ ] **Step 2: Run to verify RED.** Run:
  `python -m pytest tests/test_memory_stores_purge.py -v`
  Expected: FAIL with `AttributeError: 'InMemoryDedupeStore' object has no attribute
  'purge_expired'`.

- [ ] **Step 3: Implement.** In `src/mailflow/stores/memory.py`:

  Add `import time` to the top-level imports (after `from dataclasses import dataclass,
  field`).

  Change the `_ClaimRecord` dataclass to add the expiry field:
```python
@dataclass
class _ClaimRecord:
    claimed: bool = False
    done: bool = False
    attempts: int = 0
    expires_at: float | None = None
```

  Change `InMemoryDedupeStore.mark_done` from:
```python
    def mark_done(self, key: str, ttl_seconds: int) -> None:
        self._claims.setdefault(key, _ClaimRecord()).done = True
```
  to:
```python
    def mark_done(self, key: str, ttl_seconds: int) -> None:
        rec = self._claims.setdefault(key, _ClaimRecord())
        rec.done = True
        rec.expires_at = time.time() + ttl_seconds
```

  Add this method to `InMemoryDedupeStore` (after `release`):
```python
    def purge_expired(self, *, now: float | None = None) -> int:
        """Delete done claims past their mark_done() ttl_seconds. Call periodically
        (e.g. a scheduled job) to bound this store's memory growth in a long-running
        process — mark_done() alone does not expire anything on its own."""
        now = now if now is not None else time.time()
        expired = [
            key for key, rec in self._claims.items()
            if rec.done and rec.expires_at is not None and rec.expires_at <= now
        ]
        for key in expired:
            del self._claims[key]
        return len(expired)
```

- [ ] **Step 4: Run to verify GREEN.** Run:
  `python -m pytest tests/test_memory_stores_purge.py -v` — expected: PASS.
  Run: `python -m pytest -m "not slow" -q` — expected: no regressions (checks the
  `_ClaimRecord` field addition doesn't break `test_f01_dedupe.py` etc., since it's a new
  field with a default, not a signature change to any public method).
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/stores/memory.py tests/test_memory_stores_purge.py
git commit -m "feat(stores): InMemoryDedupeStore.purge_expired() -- mark_done ttl_seconds now actually expires"
```

---

### Task 3: TTL purge — `SqliteDedupeStore`

**Files:**
- Modify: `src/mailflow/stores/sqlite.py`
- Test: `tests/test_sqlite_stores.py` (add cases to the existing file)

**Interfaces:**
- Consumes: `self._clock: Callable[[], float]` (already exists on `SqliteDedupeStore`,
  set in `__init__`, defaults to `time.time`).
- Produces: `SqliteDedupeStore.purge_expired(self, *, now: float | None = None) -> int`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_sqlite_stores.py`:

```python
def test_purge_expired_removes_only_expired_done_claims(tmp_path) -> None:
    clock = {"now": 1_000.0}
    store = SqliteDedupeStore(str(tmp_path / "d.db"), clock=lambda: clock["now"])
    store.try_claim("expired", 300)
    store.mark_done("expired", ttl_seconds=60)   # expires at t=1060
    store.try_claim("fresh", 300)
    store.mark_done("fresh", ttl_seconds=600)    # expires at t=1600
    store.try_claim("not-done", 300)

    clock["now"] = 1_100.0
    removed = store.purge_expired()

    assert removed == 1
    assert store.try_claim("expired", 300) is True
    assert store.try_claim("fresh", 300) is False
    assert store.try_claim("not-done", 300) is False


def test_purge_expired_persists_across_new_instance(tmp_path) -> None:
    path = str(tmp_path / "d.db")
    clock = {"now": 1_000.0}
    first = SqliteDedupeStore(path, clock=lambda: clock["now"])
    first.try_claim("expired", 300)
    first.mark_done("expired", ttl_seconds=60)

    reopened = SqliteDedupeStore(path, clock=lambda: 1_100.0)
    assert reopened.purge_expired() == 1
```

- [ ] **Step 2: Run to verify RED.** Run:
  `python -m pytest tests/test_sqlite_stores.py -k purge_expired -v`
  Expected: FAIL with `AttributeError: 'SqliteDedupeStore' object has no attribute
  'purge_expired'`.

- [ ] **Step 3: Implement.** In `src/mailflow/stores/sqlite.py`:

  Change `_DEDUPE_SCHEMA` from:
```python
_DEDUPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  key TEXT PRIMARY KEY,
  done INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed INTEGER NOT NULL DEFAULT 0,
  claimed_at REAL NOT NULL DEFAULT 0
);
"""
```
  to:
```python
_DEDUPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  key TEXT PRIMARY KEY,
  done INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed INTEGER NOT NULL DEFAULT 0,
  claimed_at REAL NOT NULL DEFAULT 0,
  expires_at REAL
);
"""
```

  In `SqliteDedupeStore.__init__`, extend the existing migration check (currently only
  checking `claimed_at`) to also add `expires_at`:
```python
        self._conn.execute(_DEDUPE_SCHEMA)
        # Migrate a pre-lease/pre-ttl db (created before these columns existed).
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(claims)")}
        if "claimed_at" not in cols:
            self._conn.execute(
                "ALTER TABLE claims ADD COLUMN claimed_at REAL NOT NULL DEFAULT 0"
            )
        if "expires_at" not in cols:
            self._conn.execute("ALTER TABLE claims ADD COLUMN expires_at REAL")
        self._conn.commit()
```

  Change `mark_done` from:
```python
    def mark_done(self, key: str, ttl_seconds: int) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO claims (key) VALUES (?)", (key,))
            self._conn.execute("UPDATE claims SET done = 1 WHERE key=?", (key,))
            self._conn.commit()
```
  to:
```python
    def mark_done(self, key: str, ttl_seconds: int) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO claims (key) VALUES (?)", (key,))
            self._conn.execute(
                "UPDATE claims SET done = 1, expires_at = ? WHERE key=?",
                (self._clock() + ttl_seconds, key),
            )
            self._conn.commit()
```

  Add this method to `SqliteDedupeStore` (after `release`):
```python
    def purge_expired(self, *, now: float | None = None) -> int:
        """Delete done claims past their mark_done() ttl_seconds. Call periodically
        (e.g. a scheduled job / `mailflow purge`) to bound this table's growth."""
        now = now if now is not None else self._clock()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM claims WHERE done = 1 AND expires_at IS NOT NULL "
                "AND expires_at <= ?",
                (now,),
            )
            self._conn.commit()
            return int(cur.rowcount)
```

- [ ] **Step 4: Run to verify GREEN.** Run:
  `python -m pytest tests/test_sqlite_stores.py -v` — expected: all PASS (existing + 2 new).
  Run: `python -m pytest -m "not slow" -q` — expected: no regressions.
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/stores/sqlite.py tests/test_sqlite_stores.py
git commit -m "feat(stores): SqliteDedupeStore.purge_expired() -- mark_done ttl_seconds now actually expires"
```

---

### Task 4: TTL purge — `PostgresDedupeStore` (establishes the `ADD COLUMN IF NOT EXISTS` migration pattern)

**Files:**
- Modify: `src/mailflow/stores/postgres.py`
- Test: `tests/test_postgres_stores.py` (add cases to the existing file — same `live` +
  `MAILFLOW_TEST_POSTGRES_DSN` gating as the rest of that file)

**Interfaces:**
- Consumes: `self._clock` (already exists on `PostgresDedupeStore`).
- Produces: `PostgresDedupeStore.purge_expired(self, *, now: float | None = None) -> int`.
- This task's schema change (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) is the concrete
  migration mechanism for this store going forward — Postgres supports `IF NOT EXISTS` on
  `ADD COLUMN` natively (unlike SQLite's manual `PRAGMA table_info` check in Task 3), so
  no separate abstraction is needed; future columns follow this same one-line pattern.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_postgres_stores.py`
  (inside the existing `pytestmark`-gated file, so no new gating needed):

```python
def test_purge_expired_removes_only_expired_done_claims() -> None:
    from mailflow.stores.postgres import PostgresDedupeStore

    clock = {"now": 1_000.0}
    store = PostgresDedupeStore(DSN, clock=lambda: clock["now"])
    store.try_claim("expired", 300)
    store.mark_done("expired", ttl_seconds=60)   # expires at t=1060
    store.try_claim("fresh", 300)
    store.mark_done("fresh", ttl_seconds=600)    # expires at t=1600
    store.try_claim("not-done", 300)

    clock["now"] = 1_100.0
    removed = store.purge_expired()

    assert removed == 1
    assert store.try_claim("expired", 300) is True
    assert store.try_claim("fresh", 300) is False
    assert store.try_claim("not-done", 300) is False


def test_purge_expired_persists_across_new_instance() -> None:
    from mailflow.stores.postgres import PostgresDedupeStore

    clock = {"now": 1_000.0}
    first = PostgresDedupeStore(DSN, clock=lambda: clock["now"])
    first.try_claim("expired", 300)
    first.mark_done("expired", ttl_seconds=60)

    reopened = PostgresDedupeStore(DSN, clock=lambda: 1_100.0)
    assert reopened.purge_expired() == 1
```

- [ ] **Step 2: Run to verify RED.** Run:
  `MAILFLOW_TEST_POSTGRES_DSN=<your-local-dsn> python -m pytest tests/test_postgres_stores.py -k purge_expired -v`
  Expected: FAIL with `AttributeError: 'PostgresDedupeStore' object has no attribute
  'purge_expired'`. (If `MAILFLOW_TEST_POSTGRES_DSN` isn't set, these tests SKIP instead —
  set it to your real local Postgres DSN from earlier in this session to actually run
  Phase 1's Postgres tasks.)

- [ ] **Step 3: Implement.** In `src/mailflow/stores/postgres.py`:

  Change `_DEDUPE_SCHEMA` from:
```python
_DEDUPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  key TEXT PRIMARY KEY,
  done INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed INTEGER NOT NULL DEFAULT 0,
  claimed_at DOUBLE PRECISION NOT NULL DEFAULT 0
);
"""
```
  to:
```python
_DEDUPE_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  key TEXT PRIMARY KEY,
  done INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed INTEGER NOT NULL DEFAULT 0,
  claimed_at DOUBLE PRECISION NOT NULL DEFAULT 0,
  expires_at DOUBLE PRECISION
);
"""
```

  In `PostgresDedupeStore.__init__`, add the migration line right after
  `self._conn.execute(_DEDUPE_SCHEMA)`:
```python
        self._conn = psycopg.connect(dsn, autocommit=False)
        self._conn.execute(_DEDUPE_SCHEMA)
        self._conn.execute("ALTER TABLE claims ADD COLUMN IF NOT EXISTS expires_at DOUBLE PRECISION")
        self._conn.commit()
```

  Change `mark_done` from:
```python
    def mark_done(self, key: str, ttl_seconds: int) -> None:
        self._conn.execute(
            "INSERT INTO claims (key, done) VALUES (%s, 1) "
            "ON CONFLICT (key) DO UPDATE SET done = 1",
            (key,),
        )
        self._conn.commit()
```
  to:
```python
    def mark_done(self, key: str, ttl_seconds: int) -> None:
        expires_at = self._clock() + ttl_seconds
        self._conn.execute(
            "INSERT INTO claims (key, done, expires_at) VALUES (%s, 1, %s) "
            "ON CONFLICT (key) DO UPDATE SET done = 1, expires_at = %s",
            (key, expires_at, expires_at),
        )
        self._conn.commit()
```

  Add this method to `PostgresDedupeStore` (after `release`):
```python
    def purge_expired(self, *, now: float | None = None) -> int:
        """Delete done claims past their mark_done() ttl_seconds. Call periodically
        (e.g. a scheduled job / `mailflow purge`) to bound this table's growth."""
        now = now if now is not None else self._clock()
        cur = self._conn.execute(
            "DELETE FROM claims WHERE done = 1 AND expires_at IS NOT NULL "
            "AND expires_at <= %s",
            (now,),
        )
        self._conn.commit()
        return int(cur.rowcount)
```

- [ ] **Step 4: Run to verify GREEN.** Run:
  `MAILFLOW_TEST_POSTGRES_DSN=<dsn> python -m pytest tests/test_postgres_stores.py -v`
  Expected: all PASS (17 existing + 2 new = 19).
  Run: `MAILFLOW_TEST_POSTGRES_DSN=<dsn> python -m pytest -m "not slow" -q` — no regressions.
  Run: `python -m pytest -m "not slow" -q` (DSN unset) — same skip count as before plus 2.
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/stores/postgres.py tests/test_postgres_stores.py
git commit -m "feat(stores): PostgresDedupeStore.purge_expired() + ADD COLUMN IF NOT EXISTS migration pattern"
```

---

### Task 5: `Mailflow.purge_expired()` — expose purge through the facade

**Files:**
- Modify: `src/mailflow/facade.py` (add method to `class Mailflow`, after `health()`)
- Test: `tests/test_facade.py` (add case to the existing file)

**Interfaces:**
- Consumes: `self._dedupe_store` (already stored on `Mailflow.__init__`, used by `health()`).
- Produces: `Mailflow.purge_expired(self) -> int`.

- [ ] **Step 1: Write the failing test.** Append to `tests/test_facade.py`:

```python
def test_purge_expired_delegates_to_dedupe_store() -> None:
    from mailflow import connect
    from mailflow.core.models import StreamRef
    from mailflow.providers.memory import SeedEmail

    stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
    raw = b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: hi\r\n\r\nhello"
    mf = connect("memory", seed={stream: [SeedEmail("m1", raw)]}, tenant="acme")
    mf.fetch_new()  # processes m1 -> dedupe_store.mark_done("...", ttl_seconds=...)

    # in-memory default ttl is 60 days out, so nothing is expired yet -- purge is a no-op,
    # but this proves the method exists and returns an int without raising.
    assert mf.purge_expired() == 0


def test_purge_expired_requires_store_backed_handle() -> None:
    from mailflow import connect

    mf = connect("memory", seed={})
    mf._dedupe_store = None  # simulate a handle without a store-backed dedupe
    with pytest.raises(RuntimeError, match="purge_expired"):
        mf.purge_expired()
```

- [ ] **Step 2: Run to verify RED.** Run:
  `python -m pytest tests/test_facade.py -k purge_expired -v`
  Expected: FAIL with `AttributeError: 'Mailflow' object has no attribute 'purge_expired'`.

- [ ] **Step 3: Implement.** In `src/mailflow/facade.py`, add to `class Mailflow` right
  after the existing `health()` method:

```python
    def purge_expired(self) -> int:
        """Delete dedupe records past their done_ttl_seconds (housekeeping — call this
        periodically, e.g. from a cron/scheduled job, to bound the dedupe store's growth;
        see docs/mailflow-final-report.md for why this matters in production)."""
        if self._dedupe_store is None:
            raise RuntimeError("purge_expired() requires the store-backed handle")
        purge = getattr(self._dedupe_store, "purge_expired", None)
        if purge is None:
            raise NotImplementedError(
                f"{type(self._dedupe_store).__name__} does not support purge_expired()"
            )
        return int(purge())
```

- [ ] **Step 4: Run to verify GREEN.** Run:
  `python -m pytest tests/test_facade.py -v` — expected: all PASS.
  Run: `python -m pytest -m "not slow" -q` — no regressions.
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/facade.py tests/test_facade.py
git commit -m "feat(facade): Mailflow.purge_expired() -- app-callable dedupe-store housekeeping"
```

---

## Phase 2 — Durable dead-letter storage + redrive (`memory` + `gmail` only)

> Graph (Event Hubs) and Service Bus DLQ wiring are deferred — see the Global Constraints
> note. `core/redrive.py` and the `DeadLetterStore` port already exist and are fully
> tested (`tests/qa/test_f12_dlq.py`, `stores/test_deadletter_stores.py`); this phase only
> wires them into the public `connect()` facade, which currently never does.

### Task 6: `StoresConfig.dead_letter` field + `resolve_state()` wiring

**Files:**
- Modify: `src/mailflow/config/schema.py` (add field to `StoresConfig`)
- Modify: `src/mailflow/config/state.py` (populate it in the sqlite/postgres branches)
- Test: `tests/test_registry.py` (add cases to the existing file)

**Interfaces:**
- Produces: `StoresConfig.dead_letter: ComponentConfig` (default `kind="memory"`, matching
  `cursor`/`dedupe`/`blob`'s existing defaults).

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_registry.py`:

```python
def test_resolve_state_memory_includes_dead_letter() -> None:
    stores = resolve_state("memory")
    assert stores.dead_letter.kind == "memory"


def test_resolve_state_sqlite_includes_dead_letter() -> None:
    stores = resolve_state("sqlite:///tmp/mf.db")
    assert stores.dead_letter.kind == "sqlite"
    assert stores.dead_letter.params["path"] == "/tmp/mf.db"


def test_resolve_state_postgres_includes_dead_letter() -> None:
    dsn = "postgresql://user:pw@localhost:5432/mailflow"
    stores = resolve_state(dsn)
    assert stores.dead_letter.kind == "postgres"
    assert stores.dead_letter.params["dsn"] == dsn
```

- [ ] **Step 2: Run to verify RED.** Run:
  `python -m pytest tests/test_registry.py -k dead_letter -v`
  Expected: FAIL with `pydantic_core._pydantic_core.ValidationError` or
  `AttributeError: 'StoresConfig' object has no attribute 'dead_letter'`.

- [ ] **Step 3: Implement.** In `src/mailflow/config/schema.py`, change `StoresConfig`
  from:
```python
class StoresConfig(BaseModel):
    cursor: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    dedupe: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    blob: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
```
  to:
```python
class StoresConfig(BaseModel):
    cursor: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    dedupe: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    blob: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
    dead_letter: ComponentConfig = Field(default_factory=lambda: ComponentConfig(kind="memory"))
```

  In `src/mailflow/config/state.py`, the sqlite branch currently ends with:
```python
        return StoresConfig(
            cursor=ComponentConfig(kind="sqlite", params=dict(sqlite_params)),
            dedupe=ComponentConfig(kind="sqlite", params=dict(sqlite_params)),
            blob=ComponentConfig(kind="local", params={"directory": attach_dir}),
        )
```
  change to:
```python
        return StoresConfig(
            cursor=ComponentConfig(kind="sqlite", params=dict(sqlite_params)),
            dedupe=ComponentConfig(kind="sqlite", params=dict(sqlite_params)),
            blob=ComponentConfig(kind="local", params={"directory": attach_dir}),
            dead_letter=ComponentConfig(kind="sqlite", params=dict(sqlite_params)),
        )
```

  The postgres branch (added in the earlier Postgres plan) currently ends with:
```python
        return StoresConfig(
            cursor=ComponentConfig(kind="postgres", params=dict(postgres_params)),
            dedupe=ComponentConfig(kind="postgres", params=dict(postgres_params)),
            blob=ComponentConfig(kind="local", params={"directory": "attachments"}),
        )
```
  change to:
```python
        return StoresConfig(
            cursor=ComponentConfig(kind="postgres", params=dict(postgres_params)),
            dedupe=ComponentConfig(kind="postgres", params=dict(postgres_params)),
            blob=ComponentConfig(kind="local", params={"directory": "attachments"}),
            dead_letter=ComponentConfig(kind="postgres", params=dict(postgres_params)),
        )
```

- [ ] **Step 4: Run to verify GREEN.** Run:
  `python -m pytest tests/test_registry.py -v` — expected: all PASS.
  Run: `python -m pytest -m "not slow" -q` — no regressions (the new field has a default,
  so no existing `StoresConfig(...)` construction breaks).
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/config/schema.py src/mailflow/config/state.py tests/test_registry.py
git commit -m "feat(config): StoresConfig.dead_letter field, wired into resolve_state()"
```

---

### Task 7: Wire a durable `DeadLetterStore` through `connect()` for `memory` + `gmail`

**Files:**
- Modify: `src/mailflow/facade.py` (build `dlq_store`, pass to the memory `Pipeline(...)`
  and to `_build_gmail_live`)

**Interfaces:**
- Consumes: `mailflow.registry.build_dead_letter_store(kind, params) -> DeadLetterStore`
  (already exists); `build_gmail_runtime`/`run_service`'s existing `dlq_store` parameter
  (already threaded end-to-end in `adapters/gmail/composition.py`/`live.py` — confirmed,
  no change needed there).
- Produces: every `connect("memory"/"gmail", ...)` handle now has dead-lettered messages
  durably recorded via the resolved `state=` store, instead of a throwaway `MemoryEmitter()`.

- [ ] **Step 1: Write the failing test.** Create `tests/test_facade_dlq_durable.py`:

```python
"""connect() must wire a REAL, durable DeadLetterStore (per state=), not silently
discard dead-lettered messages into a throwaway MemoryEmitter nobody can read back."""

from __future__ import annotations

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail


def test_memory_connect_dead_letters_are_durable(tmp_path) -> None:
    stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
    # oversized message -> dead-lettered (fail-closed size guard, no filter/extractor needed)
    huge = b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: hi\r\n\r\n" + b"x" * 100
    mf = connect(
        "memory", seed={stream: [SeedEmail("m1", huge)]}, tenant="acme",
        state=f"sqlite:///{tmp_path}/mf.db",
        overrides={"max_message_bytes": 10},  # force oversized -> dead_lettered
    )
    mf.fetch_new()

    from mailflow.stores.sqlite import SqliteDeadLetterStore
    dlq = SqliteDeadLetterStore(str(tmp_path / "mf.db"))
    pending = dlq.list_pending()
    assert len(pending) == 1
    assert pending[0].reason.startswith("oversized")
```

  (This test needs `overrides={"max_message_bytes": ...}` support in `connect()` — check
  first: run `grep -n '"max_message_bytes"' src/mailflow/facade.py`. If `connect()` does
  not thread a `max_message_bytes` override into `PipelineConfig`, use this simpler
  version instead, which forces a dead-letter via an unparseable/oversized fixture without
  needing a new override — replace the test body with:)

```python
def test_memory_connect_dead_letters_are_durable(tmp_path) -> None:
    stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
    mf = connect(
        "memory", seed={stream: [SeedEmail("m1", b"")]}, tenant="acme",  # empty raw -> unparseable
        state=f"sqlite:///{tmp_path}/mf.db",
    )
    mf.fetch_new()

    from mailflow.stores.sqlite import SqliteDeadLetterStore
    dlq = SqliteDeadLetterStore(str(tmp_path / "mf.db"))
    assert len(dlq.list_pending()) == 1
```

  Try the first version; if `b""` doesn't actually dead-letter in practice (some parsers
  degrade gracefully to empty fields rather than raising), fall back to the
  `max_message_bytes` override version and add that override if it's missing — check
  `PipelineConfig` in `core/pipeline.py:61-68` for the exact field name
  (`max_message_bytes: int = 50_000_000`) and thread it from `connect()`'s existing
  `PipelineConfig(tenant=tenant, on_filtered=on_filtered)` call (memory branch) as
  `PipelineConfig(tenant=tenant, on_filtered=on_filtered, max_message_bytes=ov.get("max_message_bytes", 50_000_000))`
  if not already present.

- [ ] **Step 2: Run to verify RED.** Run:
  `python -m pytest tests/test_facade_dlq_durable.py -v`
  Expected: FAIL — `dlq.list_pending()` returns `[]` (empty), proving today's dead-lettered
  message vanished into the throwaway `MemoryEmitter()` instead of the sqlite DLQ table.

- [ ] **Step 3: Implement.** In `src/mailflow/facade.py`:

  In `connect()`, right after the existing store-building block (currently ending with
  `blob_store = ov.get("blob_store") or build_blob_store(stores.blob.kind,
  stores.blob.params)`), add:
```python
    dlq_store = ov.get("dlq_store") or build_dead_letter_store(
        stores.dead_letter.kind, stores.dead_letter.params
    )
```
  This needs `build_dead_letter_store` added to the existing `from mailflow.registry
  import (...)` block, and a `stores.dead_letter` reference — `stores` is already
  `resolve_state(state)`'s return value, already in scope.

  In the `provider == "memory"` branch, add `dlq_store=dlq_store` to the `Pipeline(...)`
  call:
```python
        pipeline = Pipeline(
            provider=ov.get("provider") or MemoryProvider(seed=seed or {}),
            parser=MimeEnvelopeParser(),
            filters=FilterChain(chain),
            extractor=MimeExtractor(attachment_policy=pol),
            emitter=pipe_emitter,
            dlq_emitter=MemoryEmitter(),
            cursor_store=cursor_store,
            dedupe_store=dedupe_store,
            blob_store=blob_store,
            cleaner=cleaner,
            config=PipelineConfig(tenant=tenant, on_filtered=on_filtered),
            observers=observers,
            dlq_store=dlq_store,
        )
```

  In `_build_gmail_live`, add a `dlq_store: Any = None` parameter and thread it to
  `run_service(...)`:
```python
def _build_gmail_live(
    *,
    credentials: dict[str, Any],
    mailbox: str | None,
    tenant: str,
    emitter: Emitter,
    secret_provider: Any,
    cursor_store: CursorStore,
    dedupe_store: Any,
    blob_store: Any,
    filters: list[Filter],
    cleaner: Any = None,
    rotation_sink: Any = None,
    verify_scope_on_startup: bool = _DEFAULT_VERIFY_SCOPE,
    attachment_policy: AttachmentPolicy | None = None,
    on_filtered: Literal["tag", "drop"] = "tag",
    observers: Observers | None = None,
    dlq_store: Any = None,
) -> Callable[[], None]:
    ...
    def live() -> None:
        run_service(
            gmail_cfg=gmail_cfg, pubsub_cfg=pubsub_cfg, tenant=tenant,
            secret_provider=secret_provider, emitter=emitter, dlq_emitter=MemoryEmitter(),
            cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
            filters=filters, cleaner=cleaner, rotation_sink=rotation_sink,
            verify_scope=verify_scope_on_startup, attachment_policy=attachment_policy,
            on_filtered=on_filtered, observers=observers, dlq_store=dlq_store,
        )

    return live
```

  In `connect()`'s `provider == "gmail"` branch, add `dlq_store=dlq_store` to the
  `_build_gmail_live(...)` call.

- [ ] **Step 4: Run to verify GREEN.** Run:
  `python -m pytest tests/test_facade_dlq_durable.py -v` — expected: PASS.
  Run: `python -m pytest -m "not slow" -q` — no regressions (check
  `tests/test_facade.py`/`test_connect_transport_selector.py` especially, since
  `_build_gmail_live`'s signature changed).
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/facade.py tests/test_facade_dlq_durable.py
git commit -m "fix(facade): wire a durable DeadLetterStore into connect() for memory+gmail (was silently discarded)"
```

---

### Task 8: CLI `mailflow redrive`

**Files:**
- Modify: `src/mailflow/cli.py` (add `redrive` subcommand + `cmd_redrive`)
- Test: `tests/test_cli_redrive.py` (create)

**Interfaces:**
- Consumes: `mailflow.config.state.resolve_state`, `mailflow.registry.
  build_dead_letter_store`/`build_blob_store`, `mailflow.core.redrive.redrive`,
  `mailflow.core.pipeline.PipelineConfig`, `mailflow.emit.stdout.StdoutEmitter`.

- [ ] **Step 1: Write the failing test.** Create `tests/test_cli_redrive.py`:

```python
"""mailflow redrive — CLI wrapper around core/redrive.py (already tested), surfaced
through the same argparse pattern as auth/check."""

from __future__ import annotations

from mailflow.cli import build_parser, main


def test_redrive_subcommand_parses() -> None:
    parser = build_parser()
    args = parser.parse_args(["redrive", "--state", "sqlite:///x.db", "--tenant", "acme"])
    assert args.command == "redrive"
    assert args.state == "sqlite:///x.db"
    assert args.tenant == "acme"
    assert args.limit is None


def test_redrive_runs_against_empty_store(tmp_path, capsys) -> None:
    db = str(tmp_path / "mf.db")
    code = main(["redrive", "--state", f"sqlite:///{db}", "--tenant", "acme"])
    assert code == 0
    out = capsys.readouterr().out
    assert "examined=0" in out
```

- [ ] **Step 2: Run to verify RED.** Run:
  `python -m pytest tests/test_cli_redrive.py -v`
  Expected: FAIL — `argparse.ArgumentError`/`SystemExit` (no `redrive` subcommand yet) or
  `AttributeError: module 'mailflow.cli' has no attribute 'main'` handling for it.

- [ ] **Step 3: Implement.** In `src/mailflow/cli.py`:

  In `build_parser()`, after the existing `check` subparser block (ends around line 62,
  right before `return parser`), add:
```python
    rd = sub.add_parser("redrive", help="re-submit durable dead-letters through the pipeline")
    rd.add_argument("--state", required=True, help="state=URI, e.g. sqlite:///mf.db")
    rd.add_argument("--tenant", required=True, help="tenant name (must match the original run)")
    rd.add_argument("--limit", type=int, default=None, help="max records to examine")

    pg = sub.add_parser("purge", help="delete dedupe records past their done ttl")
    pg.add_argument("--state", required=True, help="state=URI, e.g. sqlite:///mf.db")
```

  Add a new command section (after `cmd_check_graph`, before the `Entry` section):
```python
# ---------------------------------------------------------------------------
# Redrive / purge (operational tooling over the durable stores)
# ---------------------------------------------------------------------------

def cmd_redrive(args: argparse.Namespace) -> int:
    from mailflow.config.state import resolve_state
    from mailflow.core.pipeline import PipelineConfig
    from mailflow.core.redrive import redrive
    from mailflow.emit.stdout import StdoutEmitter
    from mailflow.registry import build_blob_store, build_dead_letter_store

    stores = resolve_state(args.state)
    dlq_store = build_dead_letter_store(stores.dead_letter.kind, stores.dead_letter.params)
    blob_store = build_blob_store(stores.blob.kind, stores.blob.params)

    report = redrive(
        store=dlq_store,
        emitter=StdoutEmitter(),
        dlq_emitter=StdoutEmitter(),
        blob_store=blob_store,
        config=PipelineConfig(tenant=args.tenant),
        limit=args.limit,
    )
    print(
        f"examined={report.examined} resubmitted={report.resubmitted} "
        f"still_dead_lettered={report.still_dead_lettered}"
    )
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    from mailflow.config.state import resolve_state
    from mailflow.registry import build_dedupe_store

    stores = resolve_state(args.state)
    dedupe_store = build_dedupe_store(stores.dedupe.kind, stores.dedupe.params)
    purge = getattr(dedupe_store, "purge_expired", None)
    if purge is None:
        print(
            f"error: {type(dedupe_store).__name__} does not support purge_expired()",
            file=sys.stderr,
        )
        return 1
    removed = purge()
    print(f"purged {removed} expired dedupe record(s)")
    return 0
```

  In `main()`, add before the final `parser.print_help(); return 1`:
```python
    if args.command == "redrive":
        return cmd_redrive(args)
    if args.command == "purge":
        return cmd_purge(args)
```

- [ ] **Step 4: Run to verify GREEN.** Run:
  `python -m pytest tests/test_cli_redrive.py -v` — expected: PASS.
  Run: `python -m pytest -m "not slow" -q` — no regressions.
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/cli.py tests/test_cli_redrive.py
git commit -m "feat(cli): add 'mailflow redrive' and 'mailflow purge' operational commands"
```

---

### Task 9: CLI `mailflow purge` test coverage

**Files:**
- Test: `tests/test_cli_redrive.py` (add case — `cmd_purge` was implemented in Task 8;
  this task is just its dedicated test, kept separate so Task 8's review isn't blocked on
  a second command's test design)

- [ ] **Step 1: Write the failing test.** Append to `tests/test_cli_redrive.py`:

```python
def test_purge_runs_against_empty_store(tmp_path, capsys) -> None:
    from mailflow.cli import main

    db = str(tmp_path / "mf.db")
    code = main(["purge", "--state", f"sqlite:///{db}"])
    assert code == 0
    assert "purged 0 expired" in capsys.readouterr().out


def test_purge_actually_removes_expired_records(tmp_path, capsys) -> None:
    from mailflow.cli import main
    from mailflow.stores.sqlite import SqliteDedupeStore

    db = str(tmp_path / "mf.db")
    store = SqliteDedupeStore(db, clock=lambda: 1_000.0)
    store.try_claim("k1", 300)
    store.mark_done("k1", ttl_seconds=1)  # expires at t=1001, already in the past by "now"

    code = main(["purge", "--state", f"sqlite:///{db}"])
    assert code == 0
    assert "purged 1 expired" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify RED.** This depends on Task 8 already being implemented —
  if Task 8 is done first, these should actually be closer to green already; run:
  `python -m pytest tests/test_cli_redrive.py -k purge -v` and confirm the SECOND test
  (`test_purge_actually_removes_expired_records`) is the meaningful one — if it fails
  because `SqliteDedupeStore(db, clock=lambda: 1_000.0)` then `main(...)` re-resolves
  `time.time()` (the real clock) as "now" (since the CLI doesn't inject a clock), the
  `mark_done` expiry (`1_000 + 1 = 1001`) will already be far in the past relative to the
  real current time — so this test should PASS once Task 8's `cmd_purge` exists. If it's
  already green immediately after Task 8, that's fine (this task is coverage, not new
  behavior) — just confirm it fails correctly if Task 8 is reverted, to prove it isn't
  vacuous: temporarily comment out the `if args.command == "purge":` dispatch line in
  `main()`, confirm both tests fail, then restore it.

- [ ] **Step 3: No implementation needed** — `cmd_purge` already exists from Task 8.

- [ ] **Step 4: Run to verify GREEN.** Run:
  `python -m pytest tests/test_cli_redrive.py -v` — expected: all 4 PASS.
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add tests/test_cli_redrive.py
git commit -m "test(cli): dedicated coverage for 'mailflow purge' removing expired records"
```

---

## Phase 3 — Resilience & Postgres hardening

### Task 10: Startup connection retry/backoff for `Sqlite*`/`Postgres*` store constructors

**Files:**
- Create: `src/mailflow/stores/_retry.py`
- Modify: `src/mailflow/stores/postgres.py` (wrap `psycopg.connect` calls)
- Test: `tests/test_stores_retry.py` (create)

**Interfaces:**
- Produces: `connect_with_retry(connect_fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 0.5, sleep: Callable[[float], None] = time.sleep) -> T` — raises the last exception if all attempts fail.
- SQLite is a local file (no network), so it doesn't need this — only `PostgresCursorStore`/`PostgresDedupeStore`/`PostgresDeadLetterStore` use it (a transient network blip to a real Postgres server is the actual scenario this protects against).

- [ ] **Step 1: Write the failing test.** Create `tests/test_stores_retry.py`:

```python
"""connect_with_retry(): retries a flaky connect callable with backoff, then gives up
and raises the last error -- used by the Postgres stores at startup."""

from __future__ import annotations

import pytest

from mailflow.stores._retry import connect_with_retry


def test_succeeds_on_first_try() -> None:
    calls = []

    def connect() -> str:
        calls.append(1)
        return "ok"

    assert connect_with_retry(connect, attempts=3, sleep=lambda _: None) == "ok"
    assert len(calls) == 1


def test_retries_then_succeeds() -> None:
    calls = []

    def connect() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("transient")
        return "ok"

    slept = []
    result = connect_with_retry(connect, attempts=3, sleep=slept.append)
    assert result == "ok"
    assert len(calls) == 3
    assert len(slept) == 2  # slept between attempt 1->2 and 2->3, not after the last success


def test_gives_up_after_max_attempts() -> None:
    def connect() -> str:
        raise ConnectionError("always fails")

    with pytest.raises(ConnectionError, match="always fails"):
        connect_with_retry(connect, attempts=3, sleep=lambda _: None)
```

- [ ] **Step 2: Run to verify RED.** Run:
  `python -m pytest tests/test_stores_retry.py -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.stores._retry'`.

- [ ] **Step 3: Implement.** Create `src/mailflow/stores/_retry.py`:

```python
"""A small, dependency-free retry-with-backoff helper for store constructors that open a
real network connection (Postgres) — not needed by SQLite (a local file, no network)."""

from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def connect_with_retry(
    connect_fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call connect_fn() up to `attempts` times, with exponential backoff between
    attempts (base_delay, base_delay*2, ...). Raises the last exception if every
    attempt fails. Protects against a momentarily-unreachable Postgres server at
    process startup (e.g. a rolling restart) without needing an external dependency."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return connect_fn()
        except Exception as exc:  # noqa: BLE001 - any connect failure is retryable here
            last_exc = exc
            if attempt < attempts - 1:
                sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc
```

- [ ] **Step 4: Run to verify GREEN.** Run:
  `python -m pytest tests/test_stores_retry.py -v` — expected: all 3 PASS.
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/stores/_retry.py tests/test_stores_retry.py
git commit -m "feat(stores): connect_with_retry() -- backoff helper for network-backed store constructors"
```

- [ ] **Step 6: Write the failing test for Postgres actually using it.** Append to
  `tests/test_postgres_stores.py`:

```python
def test_cursor_store_retries_transient_connect_failure(monkeypatch) -> None:
    import psycopg

    from mailflow.stores.postgres import PostgresCursorStore

    real_connect = psycopg.connect
    calls = {"n": 0}

    def flaky_connect(dsn: str, **kwargs: object):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 2:
            raise psycopg.OperationalError("transient")
        return real_connect(dsn, **kwargs)

    monkeypatch.setattr(psycopg, "connect", flaky_connect)
    store = PostgresCursorStore(DSN)  # must retry once internally, not raise
    assert calls["n"] == 2
    assert store.get(TENANT, STREAM) is None
```

- [ ] **Step 7: Run to verify RED.** Run:
  `MAILFLOW_TEST_POSTGRES_DSN=<dsn> python -m pytest tests/test_postgres_stores.py -k retries_transient -v`
  Expected: FAIL — `psycopg.OperationalError: transient` propagates immediately (no retry
  today).

- [ ] **Step 8: Implement.** In `src/mailflow/stores/postgres.py`, add the import:
```python
from mailflow.stores._retry import connect_with_retry
```
  Change each of the three constructors' connect call. `PostgresCursorStore.__init__`
  from:
```python
        import psycopg  # local import: `postgres` extra only needed if this runs

        self.dsn = dsn
        self._conn = psycopg.connect(dsn, autocommit=False)
```
  to:
```python
        import psycopg  # local import: `postgres` extra only needed if this runs

        self.dsn = dsn
        self._conn = connect_with_retry(lambda: psycopg.connect(dsn, autocommit=False))
```
  Apply the identical change to `PostgresDedupeStore.__init__` and
  `PostgresDeadLetterStore.__init__` (same `import psycopg` / `self._conn = psycopg.
  connect(dsn, autocommit=False)` lines in each).

- [ ] **Step 9: Run to verify GREEN.** Run:
  `MAILFLOW_TEST_POSTGRES_DSN=<dsn> python -m pytest tests/test_postgres_stores.py -v`
  Expected: all PASS (19 + 1 = 20).
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 10: Commit.**

```bash
git add src/mailflow/stores/postgres.py tests/test_postgres_stores.py
git commit -m "fix(stores): Postgres store constructors retry transient connect failures at startup"
```

---

### Task 11: Postgres DSN secret-ref resolution (`connect(..., state_ref=...)`)

**Files:**
- Modify: `src/mailflow/facade.py` (add `state_ref` parameter to `connect()`)
- Test: `tests/test_facade_state_ref.py` (create)

**Interfaces:**
- Consumes: the existing `SecretProvider` port (`get(ref: str) -> str`) and
  `EnvSecretProvider` (already imported in `facade.py`).
- Produces: `connect(..., state: str = "memory", state_ref: str | None = None, ...)` —
  when `state_ref` is given, it's resolved via `secret_provider.get(state_ref)` to obtain
  the actual `state=` DSN, so a real DSN/password never has to appear directly in
  application code or a plain `state=` literal — only in wherever `secret_provider`
  resolves refs from (env var by default, same as `GMAIL_CLIENT_SECRET`/
  `GRAPH_CLIENT_SECRET` already do via `"env://..."` refs).

- [ ] **Step 1: Write the failing test.** Create `tests/test_facade_state_ref.py`:

```python
"""connect(state_ref=...) resolves the state= DSN via SecretProvider at connect time,
so a Postgres password never has to be embedded literally in application code."""

from __future__ import annotations

import os

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail


def test_state_ref_resolves_via_secret_provider(tmp_path, monkeypatch) -> None:
    dsn = f"sqlite:///{tmp_path}/mf.db"
    monkeypatch.setenv("MAILFLOW_TEST_STATE_DSN", dsn)

    stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
    raw = b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: hi\r\n\r\nhello"
    mf = connect(
        "memory", seed={stream: [SeedEmail("m1", raw)]}, tenant="acme",
        state_ref="env://MAILFLOW_TEST_STATE_DSN",
    )
    mf.fetch_new()

    from mailflow.stores.sqlite import SqliteCursorStore

    cursor_store = SqliteCursorStore(str(tmp_path / "mf.db"))
    assert cursor_store.get("acme", stream) is not None  # proves sqlite state was actually used


def test_state_ref_takes_precedence_over_state() -> None:
    # state="memory" (the default) would be silently used if state_ref were ignored --
    # this proves state_ref, when given, wins.
    mf = connect(
        "memory", seed={}, tenant="acme", state="memory",
        state_ref="env://__MAILFLOW_NONEXISTENT_REF__",
    )
    # __MAILFLOW_NONEXISTENT_REF__ isn't set -> EnvSecretProvider.get() should raise,
    # proving state_ref was actually consulted instead of silently falling back to "memory".
```

  (The second test's exact assertion depends on `EnvSecretProvider.get()`'s behavior for
  a missing env var — check `src/mailflow/secrets.py` for whether it raises `KeyError` or
  something else, and wrap the `connect(...)` call in the matching `pytest.raises(...)`.)

- [ ] **Step 2: Run to verify RED.** Run:
  `python -m pytest tests/test_facade_state_ref.py -v`
  Expected: FAIL with `TypeError: connect() got an unexpected keyword argument 'state_ref'`.

- [ ] **Step 3: Implement.** In `src/mailflow/facade.py`, add `state_ref` to the `connect()`
  signature, right after `state: str = "memory",`:
```python
    state: str = "memory",
    state_ref: str | None = None,
```
  Then, at the very top of `connect()`'s body — before the existing `stores =
  resolve_state(state)` line — resolve it:
```python
    if state_ref is not None:
        state = (secret_provider or EnvSecretProvider()).get(state_ref)
    stores = resolve_state(state)
```
  (This must run before `secret_provider` is otherwise used/defaulted later in the
  function — check the existing code for where `secret_provider` is first referenced, e.g.
  in the `gmail`/`graph` branches as `secret_provider or EnvSecretProvider()`, and make sure
  this new line at the top doesn't conflict; it constructs its own `EnvSecretProvider()`
  fallback locally rather than mutating the `secret_provider` parameter, so the later
  `secret_provider or EnvSecretProvider()` calls elsewhere in `connect()` are unaffected.)

- [ ] **Step 4: Run to verify GREEN.** Run:
  `python -m pytest tests/test_facade_state_ref.py -v` — expected: PASS.
  Run: `python -m pytest -m "not slow" -q` — no regressions.
  Run: `python -m mypy` — expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/facade.py tests/test_facade_state_ref.py
git commit -m "feat(facade): connect(state_ref=...) -- resolve the state= DSN via SecretProvider"
```

  Then update `docs/gcp-postgres-setup.md`'s §6 "Secrets" section to mention
  `state_ref="env://POSTGRES_DSN"` as the now-available code-level option (in addition to
  the platform-level Secret-Manager-as-env-var injection already documented there) — this
  is a doc touch-up, no separate commit needed; fold it into this commit.

---

## Phase 4 — Packaging (light touch, per user's steer)

### Task 12: `py.typed` marker

**Files:**
- Create: `src/mailflow/py.typed` (empty file)
- Modify: `pyproject.toml` (ensure the wheel includes it — hatchling includes all files
  under the package dir by default, so no config change should be needed, but verify)

- [ ] **Step 1: Create the marker.** Create `src/mailflow/py.typed` with empty content
  (a zero-byte file is the correct, standard form — PEP 561).

- [ ] **Step 2: Verify it's picked up.** Run:
  `python -m pip install -e . --no-deps --force-reinstall -q && python -c "import mailflow, os; print(os.path.exists(os.path.join(os.path.dirname(mailflow.__file__), 'py.typed')))"`
  Expected: prints `True`.

- [ ] **Step 3: Commit.**

```bash
git add src/mailflow/py.typed
git commit -m "chore(packaging): add py.typed marker (PEP 561) so consumers get mypy/pyright type checking"
```

---

### Task 13: Dependency version upper bounds

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add upper bounds.** In `src/mailflow`'s `pyproject.toml`, change:
```toml
dependencies = ["pydantic>=2.6"]
```
  to:
```toml
dependencies = ["pydantic>=2.6,<3"]
```
  and change the extras similarly (add a `<N` ceiling one major version above the current
  floor for each, matching this exact pattern):
```toml
dev = ["mypy>=1.9,<2", "pytest>=8,<9", "hypothesis>=6,<7"]
graph = ["msal>=1.28,<2", "httpx>=0.27,<1", "azure-eventhub>=5.11,<6", "azure-eventhub-checkpointstoreblob>=1.1,<2", "azure-storage-blob>=12.19,<13", "azure-identity>=1.16,<2"]
gmail = ["httpx>=0.27,<1", "google-auth>=2.30,<3", "google-auth-oauthlib>=1.2,<2", "google-api-python-client>=2.130,<3", "google-cloud-pubsub>=2.21,<3"]
servicebus = ["azure-servicebus>=7.12,<8", "azure-identity>=1.16,<2"]
postgres = ["psycopg[binary]>=3.1,<4"]
```

- [ ] **Step 2: Verify nothing breaks.** Run: `python -m pip install -e . -q` (re-resolves
  against the new bounds — should succeed silently since all currently-installed versions
  are within range). Run: `python -m pytest -m "not slow" -q` — expected: no regressions
  (this is a metadata-only change). Run: `python -m mypy` — expected: clean.

- [ ] **Step 3: Commit.**

```bash
git add pyproject.toml
git commit -m "chore(packaging): pin dependency upper bounds (major-version ceilings)"
```

---

### Task 14: Minimal CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create the workflow.** Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: ["**"]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install
        run: pip install -e ".[dev]"
      - name: Test (fast tier)
        run: python -m pytest -m "not slow" -q
      - name: Type check
        run: python -m mypy
```

  This intentionally runs only the fast/offline tier (`not slow`), matching what this
  session ran repeatedly throughout — no Postgres/live-marked tests (those need real
  infra, out of scope for a basic CI gate per this plan's focus).

- [ ] **Step 2: Verify locally (can't run GitHub Actions from here, but validate the
  commands it runs match what this repo's CLAUDE.md/testing-guide document).** Run:
  `python -m pytest -m "not slow" -q` and `python -m mypy` one more time locally — confirm
  both are exactly the commands the workflow runs, so pushing it doesn't immediately fail
  on a command mismatch.

- [ ] **Step 3: Commit.**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add minimal GitHub Actions workflow (fast pytest tier + mypy)"
```

---

## Self-Review

1. **Spec coverage:** all 8 findings from the 2026-07-13 review are covered — TTL purge
   (Tasks 2-4), ConfigError (Task 1), durable DLQ + redrive (Tasks 6-9), startup retry
   (Task 10), Postgres migration pattern (folded into Task 4) + secret-ref DSN (Task 11),
   packaging (Tasks 12-14, license/CHANGELOG/Dockerfile deliberately excluded per the
   user's explicit steer). Service Bus/Event Hub configuration is untouched throughout,
   per the Global Constraints.
2. **Placeholder scan:** every step has complete code; Task 7 and Task 9 each carry one
   explicit contingency note (which of two test variants to use, and how to prove Task 9's
   test isn't vacuous) because they depend on details (whether `max_message_bytes` is
   already overridable, exact timing behavior) that should be confirmed against the live
   codebase at execution time rather than guessed — both branches are fully written out,
   not deferred.
3. **Type consistency:** `purge_expired(self, *, now: float | None = None) -> int` is
   spelled identically across `InMemoryDedupeStore`/`SqliteDedupeStore`/
   `PostgresDedupeStore` (Tasks 2-4) and `Mailflow.purge_expired(self) -> int` (Task 5)
   delegates to it via `getattr`. `dlq_store` parameter name/type matches between
   `_build_gmail_live` (Task 7), `build_gmail_runtime`, and `run_service` (both already
   existing, confirmed by reading the source before writing this plan). `connect_with_retry`
   (Task 10) is used identically in all three Postgres store constructors.

**Verification points flagged inline:** (a) Task 7's dead-letter test needs confirming
whether `max_message_bytes` is already an available override — check before writing the
final test; (b) Task 9's second test's timing assumption (CLI purge uses the real clock,
not an injected one) should be sanity-checked against Task 8's actual `cmd_purge`
implementation once written; (c) Task 11's second test's exact exception type depends on
`EnvSecretProvider.get()`'s current behavior for a missing ref — check `secrets.py` first.

---

Plan complete and saved to `docs/superpowers/plans/2026-07-13-mailflow-production-hardening.md`.
