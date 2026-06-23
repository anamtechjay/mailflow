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

from mailflow.core.models import Cursor, StreamRef

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
  attempts INTEGER NOT NULL DEFAULT 0
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
    """

    def __init__(self, db_path: str = "mailflow.db") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(_DEDUPE_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def try_claim(self, key: str, lease_seconds: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO claims (key) VALUES (?)", (key,)
            )
            self._conn.commit()
            return cur.rowcount == 1  # exactly one writer wins the create

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
            self._conn.execute("UPDATE claims SET done = 1 WHERE key=?", (key,))
            self._conn.commit()

    def release(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM claims WHERE key=? AND done=0", (key,))
            self._conn.commit()
