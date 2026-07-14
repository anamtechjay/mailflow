"""PostgreSQL state stores — the restart-safe AND multi-process-safe cursor + dedupe
adapters. Mirror `stores/sqlite.py` method-for-method (same CursorStore / DedupeStore /
DeadLetterStore ports, same §8 contracts), but claim atomicity comes from Postgres's own
`INSERT ... ON CONFLICT ... RETURNING` transaction semantics rather than an in-process
`threading.Lock` — SQLite's lock only protects one process; Postgres is the backend for
when multiple real workers/hosts share one dedupe store (spec §8.2 across replicas).

`psycopg` is imported only inside this module (never at package top-level), matching
how the Gmail/Graph vendor SDKs stay local to their adapters — importing `mailflow`
doesn't require the `postgres` extra unless this module is actually used.

They store ONLY the library's own bookkeeping (a per-stream cursor + per-message claim
keys) — never the emails themselves, same as every other store in this package.
"""

from __future__ import annotations

import time
from typing import Callable

from mailflow.core.models import Cursor, StreamRef
from mailflow.core.observability import DeadLetterRecord
from mailflow.stores._retry import connect_with_retry

_CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (
  tenant TEXT NOT NULL,
  stream TEXT NOT NULL,
  value  TEXT NOT NULL,
  order_n BIGINT NOT NULL,
  PRIMARY KEY (tenant, stream)
);
"""

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

_DEADLETTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS dead_letters (
  record_id TEXT PRIMARY KEY,
  payload   TEXT NOT NULL
);
"""


class PostgresCursorStore:
    """Per-(tenant, stream) cursor with monotonic compare-and-set (spec §8.3)."""

    def __init__(self, dsn: str) -> None:
        import psycopg  # local import: `postgres` extra only needed if this runs

        self.dsn = dsn
        self._conn = connect_with_retry(lambda: psycopg.connect(dsn, autocommit=False))
        self._conn.execute(_CURSOR_SCHEMA)
        self._conn.commit()

    def get(self, tenant: str, stream: StreamRef) -> Cursor | None:
        row = self._conn.execute(
            "SELECT value, order_n FROM cursors WHERE tenant=%s AND stream=%s",
            (tenant, stream.key),
        ).fetchone()
        if row is None:
            return None
        return Cursor(value=str(row[0]), order=int(row[1]))

    def commit_if_ahead(self, tenant: str, stream: StreamRef, cursor: Cursor) -> bool:
        cur = self._conn.execute(
            "INSERT INTO cursors (tenant, stream, value, order_n) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tenant, stream) DO UPDATE SET value=excluded.value, "
            "order_n=excluded.order_n WHERE cursors.order_n < excluded.order_n",
            (tenant, stream.key, cursor.value, cursor.order),
        )
        self._conn.commit()
        return cur.rowcount == 1


class PostgresDedupeStore:
    """Atomic claim-before-work (spec §8.2) + per-claim attempt counter (spec §8.4).

    Same CONTRACT as `SqliteDedupeStore`: `attempts` is a LIFETIME counter for the key
    (survives release+re-claim); `release()` clears only the `claimed` flag and keeps
    the row. `try_claim` is atomic across independent connections/processes via a single
    `INSERT ... ON CONFLICT DO UPDATE ... WHERE` statement + Postgres's row-level locking
    -- no in-process lock is involved, unlike the SQLite/in-memory stores.
    """

    def __init__(
        self, dsn: str, *, clock: Callable[[], float] = time.time
    ) -> None:
        import psycopg

        self.dsn = dsn
        self._clock = clock
        self._conn = connect_with_retry(lambda: psycopg.connect(dsn, autocommit=False))
        self._conn.execute(_DEDUPE_SCHEMA)
        self._conn.execute(
            "ALTER TABLE claims ADD COLUMN IF NOT EXISTS expires_at DOUBLE PRECISION"
        )
        self._conn.commit()

    def try_claim(self, key: str, lease_seconds: int) -> bool:
        now = self._clock()
        # One statement, one round-trip: insert if absent; if present, claim only when
        # not done AND (not currently claimed OR its lease has expired). Postgres
        # evaluates the WHERE against the pre-existing row under the row lock the
        # UPSERT already takes, so two concurrent connections can't both win.
        cur = self._conn.execute(
            "INSERT INTO claims (key, claimed, claimed_at) VALUES (%s, 1, %s) "
            "ON CONFLICT (key) DO UPDATE SET claimed = 1, claimed_at = %s "
            "WHERE claims.done = 0 AND (claims.claimed = 0 "
            "OR %s - claims.claimed_at >= %s)",
            (key, now, now, now, lease_seconds),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def record_attempt(self, key: str) -> int:
        self._conn.execute(
            "INSERT INTO claims (key) VALUES (%s) ON CONFLICT (key) DO NOTHING", (key,)
        )
        row = self._conn.execute(
            "UPDATE claims SET attempts = attempts + 1 WHERE key=%s RETURNING attempts",
            (key,),
        ).fetchone()
        self._conn.commit()
        assert row is not None
        return int(row[0])

    def mark_done(self, key: str, ttl_seconds: int) -> None:
        expires_at = self._clock() + ttl_seconds
        self._conn.execute(
            "INSERT INTO claims (key, done, expires_at) VALUES (%s, 1, %s) "
            "ON CONFLICT (key) DO UPDATE SET done = 1, expires_at = %s",
            (key, expires_at, expires_at),
        )
        self._conn.commit()

    def release(self, key: str) -> None:
        self._conn.execute(
            "UPDATE claims SET claimed = 0 WHERE key=%s AND done=0", (key,)
        )
        self._conn.commit()

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


class PostgresDeadLetterStore:
    """Durable, replayable DLQ records in a single Postgres table (one JSON payload
    per record_id). Mirrors `SqliteDeadLetterStore` method-for-method."""

    def __init__(self, dsn: str) -> None:
        import psycopg

        self.dsn = dsn
        self._conn = connect_with_retry(lambda: psycopg.connect(dsn, autocommit=False))
        self._conn.execute(_DEADLETTER_SCHEMA)
        self._conn.commit()

    def put(self, record: DeadLetterRecord) -> None:
        self._conn.execute(
            "INSERT INTO dead_letters (record_id, payload) VALUES (%s, %s) "
            "ON CONFLICT (record_id) DO UPDATE SET payload=excluded.payload",
            (record.record_id, record.model_dump_json()),
        )
        self._conn.commit()

    def list_pending(self, *, limit: int | None = None) -> list[DeadLetterRecord]:
        sql = "SELECT payload FROM dead_letters ORDER BY record_id"
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT %s"
            params = (limit,)
        rows = self._conn.execute(sql, params).fetchall()
        return [DeadLetterRecord.model_validate_json(str(row[0])) for row in rows]

    def delete(self, record_id: str) -> None:
        self._conn.execute("DELETE FROM dead_letters WHERE record_id=%s", (record_id,))
        self._conn.commit()
