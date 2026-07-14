"""Persistent SQLite state stores — the restart-safe cursor + dedupe adapters.

These back the §8 invariants with a single .db file (no server), mirroring the
in-memory stores in `stores/memory.py` method-for-method so they satisfy the same
CursorStore / DedupeStore ports. One connection (`check_same_thread=False`) guarded by
a `threading.Lock`, matching `persistence/sqlite_store.py`.

They store ONLY the library's own bookkeeping (a per-stream cursor + per-message claim
keys) — never the emails themselves. Email storage is the consuming app's job.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Callable

from mailflow.core.models import Cursor, StreamRef
from mailflow.core.observability import DeadLetterRecord

_CURSOR_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (
  tenant TEXT NOT NULL,
  stream TEXT NOT NULL,
  value  TEXT NOT NULL,
  order_n INTEGER NOT NULL,
  PRIMARY KEY (tenant, stream)
);
"""

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


_DEADLETTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS dead_letters (
  record_id TEXT PRIMARY KEY,
  payload   TEXT NOT NULL
);
"""


class SqliteCursorStore:
    """Per-(tenant, stream) cursor with monotonic compare-and-set (spec §8.3)."""

    def __init__(self, db_path: str = "mailflow.db") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(_CURSOR_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def get(self, tenant: str, stream: StreamRef) -> Cursor | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value, order_n FROM cursors WHERE tenant=? AND stream=?",
                (tenant, stream.key),
            ).fetchone()
        if row is None:
            return None
        return Cursor(value=str(row[0]), order=int(row[1]))

    def commit_if_ahead(self, tenant: str, stream: StreamRef, cursor: Cursor) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT order_n FROM cursors WHERE tenant=? AND stream=?",
                (tenant, stream.key),
            ).fetchone()
            if row is not None and cursor.order <= int(row[0]):
                return False  # forward-only: reject stale/equal
            self._conn.execute(
                "INSERT INTO cursors (tenant, stream, value, order_n) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tenant, stream) DO UPDATE SET value=excluded.value, "
                "order_n=excluded.order_n",
                (tenant, stream.key, cursor.value, cursor.order),
            )
            self._conn.commit()
            return True


class SqliteDedupeStore:
    """Atomic claim-before-work (spec §8.2) + per-claim attempt counter (spec §8.4).

    Lease/TTL expiry is not simulated (parity with the in-memory store); the shape is
    kept exact so a real Firestore/Redis adapter can add expiry without changing callers.

    CONTRACT (REL-3 / finding I5+P1): `attempts` is a LIFETIME counter for the key,
    not a per-in-process-claim counter. `release()` clears only the `claimed` flag
    (via `UPDATE ... SET claimed = 0`) and KEEPS the row (including `attempts`) --
    it no longer `DELETE`s it. This mirrors `stores/memory.py`'s fix: without it,
    every redelivery (a fresh `try_claim` after a crash/worker restart) would have
    recreated the row at `attempts=0`, so `PipelineConfig.max_attempts` could never
    be reached across separate runs and a persistently-failing message would retry
    forever instead of reaching the DLQ.

    CONTRACT (REL-2 / DEP-7 lease expiry): `try_claim` stamps `claimed_at` from an
    injectable wall clock (`clock`, default `time.time`). A claim is exclusive only
    for `lease_seconds`; a row that is `claimed=1, done=0` but whose lease has elapsed
    (`now - claimed_at >= lease_seconds`) is reclaimable. This is what lets a worker
    that crashed BETWEEN `try_claim` and `mark_done` be recovered: the stale claim
    is honored (no double-processing) until the lease expires, then reprocessed. A
    `done=1` row is never reclaimable, no matter how much time passes. Unlike the
    in-memory store (single-process, no expiry needed), the sqlite store survives a
    real process restart, so it uses wall time — not `time.monotonic` — for the lease.
    """

    def __init__(
        self, db_path: str = "mailflow.db", *, clock: Callable[[], float] = time.time
    ) -> None:
        self.db_path = db_path
        self._clock = clock
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(_DEDUPE_SCHEMA)
        # Migrate a pre-lease/pre-ttl db (created before these columns existed).
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(claims)")}
        if "claimed" not in cols:
            self._conn.execute(
                "ALTER TABLE claims ADD COLUMN claimed INTEGER NOT NULL DEFAULT 0"
            )
        if "claimed_at" not in cols:
            self._conn.execute(
                "ALTER TABLE claims ADD COLUMN claimed_at REAL NOT NULL DEFAULT 0"
            )
        if "expires_at" not in cols:
            self._conn.execute("ALTER TABLE claims ADD COLUMN expires_at REAL")
        self._conn.commit()
        self._lock = threading.Lock()

    def try_claim(self, key: str, lease_seconds: int) -> bool:
        now = self._clock()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO claims (key, claimed, claimed_at) VALUES (?, 1, ?)",
                (key, now),
            )
            if cur.rowcount == 1:  # exactly one writer wins the create
                self._conn.commit()
                return True
            # Row already existed (a prior claim/attempt/done record): claimable only
            # if it's not done AND either not currently claimed or its lease has expired
            # (a worker crashed mid-message and never released — REL-2).
            cur = self._conn.execute(
                "UPDATE claims SET claimed = 1, claimed_at = ? "
                "WHERE key=? AND done = 0 AND (claimed = 0 OR ? - claimed_at >= ?)",
                (now, key, now, lease_seconds),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def record_attempt(self, key: str) -> int:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO claims (key) VALUES (?)", (key,))
            self._conn.execute(
                "UPDATE claims SET attempts = attempts + 1 WHERE key=?", (key,)
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT attempts FROM claims WHERE key=?", (key,)
            ).fetchone()
        return int(row[0])

    def mark_done(self, key: str, ttl_seconds: int) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO claims (key) VALUES (?)", (key,))
            self._conn.execute(
                "UPDATE claims SET done = 1, expires_at = ? WHERE key=?",
                (self._clock() + ttl_seconds, key),
            )
            self._conn.commit()

    def release(self, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE claims SET claimed = 0 WHERE key=? AND done=0", (key,)
            )
            self._conn.commit()

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
            self._conn.execute(
                "DELETE FROM dead_letters WHERE record_id=?", (record_id,)
            )
            self._conn.commit()
