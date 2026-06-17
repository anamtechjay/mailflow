"""SqliteEmailStore — persist received CleanEmails to a local SQLite database.

A simple, zero-setup SQL store for the consuming app: one .db file, no server. Keyed
on canonical_id (INSERT OR IGNORE) so re-delivered emails are stored once. Swap for a
Postgres-backed store later behind the same save()/count()/recent() shape.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_id TEXT UNIQUE,
  tenant TEXT, provider TEXT, provider_message_id TEXT, provider_stream_id TEXT,
  direction TEXT, subject TEXT,
  from_name TEXT, from_address TEXT,
  to_addresses TEXT, cc_addresses TEXT,
  date_utc TEXT, received_at TEXT,
  body_text TEXT, body_html TEXT,
  attachment_count INTEGER, attachments_json TEXT,
  folder TEXT, labels TEXT, categories TEXT,
  message_size_bytes INTEGER, schema_version TEXT,
  raw_json TEXT, thread_key TEXT, read INTEGER DEFAULT 0,
  stored_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_COLUMNS = (
    "canonical_id,tenant,provider,provider_message_id,provider_stream_id,direction,"
    "subject,from_name,from_address,to_addresses,cc_addresses,date_utc,received_at,"
    "body_text,body_html,attachment_count,attachments_json,folder,labels,categories,"
    "message_size_bytes,schema_version,raw_json,thread_key,read"
)


def _thread_key(email: dict[str, Any]) -> str:
    """A stable grouping key for a conversation: the root Message-ID (oldest entry in
    References), else this message's own canonical_id (it IS the root)."""
    refs = email.get("references") or []
    if refs:
        return str(refs[0])
    return str(email.get("canonical_id") or "")


class SqliteEmailStore:
    def __init__(self, db_path: str = "emails.db") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_SCHEMA)
        # migrate older DBs that predate thread_key
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(emails)")]
        if "thread_key" not in cols:
            self._conn.execute("ALTER TABLE emails ADD COLUMN thread_key TEXT")
        if "read" not in cols:
            self._conn.execute("ALTER TABLE emails ADD COLUMN read INTEGER DEFAULT 0")
        self._conn.commit()
        self._lock = threading.Lock()

    def save(self, event: dict[str, Any]) -> bool:
        """Store one EmailEvent dict (as published to the queue). Returns True if a new
        row was inserted, False if it was a duplicate (same canonical_id)."""
        e = event.get("email", {}) or {}
        frm = e.get("from", {}) or {}

        def addrs(key: str) -> str:
            return json.dumps([r.get("address", "") for r in (e.get(key) or [])])

        row = (
            e.get("canonical_id"), event.get("tenant"), e.get("provider"),
            e.get("provider_message_id"), e.get("provider_stream_id"),
            e.get("direction"), e.get("subject"),
            frm.get("name"), frm.get("address"),
            addrs("to"), addrs("cc"),
            e.get("date_utc"), e.get("received_at"),
            e.get("body_text"), e.get("body_html"),
            len(e.get("attachments") or []), json.dumps(e.get("attachments") or []),
            e.get("folder"), json.dumps(e.get("labels") or []), json.dumps(e.get("categories") or []),
            e.get("message_size_bytes"), e.get("schema_version"),
            json.dumps(event), _thread_key(e), 0,
        )
        placeholders = ",".join(["?"] * len(_COLUMNS.split(",")))
        with self._lock:
            cur = self._conn.execute(
                f"INSERT OR IGNORE INTO emails ({_COLUMNS}) VALUES ({placeholders})", row
            )
            self._conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM emails")
            return int(cur.fetchone()[0])

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id,canonical_id,direction,subject,from_address,date_utc,"
                "attachment_count,stored_at FROM emails ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def threads(self, limit: int = 30) -> list[dict[str, Any]]:
        """Group stored emails into conversations. Returns newest-active threads first;
        within each thread, messages are ordered oldest -> newest (1st, 2nd, …)."""
        with self._lock:
            heads = self._conn.execute(
                "SELECT thread_key, COUNT(*) AS c, MAX(date_utc) AS last "
                "FROM emails GROUP BY thread_key ORDER BY last DESC LIMIT ?",
                (limit,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for h in heads:
                rows = self._conn.execute(
                    "SELECT canonical_id,direction,subject,from_name,from_address,"
                    "date_utc,body_text,attachments_json FROM emails "
                    "WHERE thread_key IS ? ORDER BY date_utc ASC, id ASC",
                    (h["thread_key"],),
                ).fetchall()
                messages = [
                    {
                        "seq": i + 1,
                        "from": f"{r['from_name'] or ''} <{r['from_address'] or ''}>".strip(),
                        "direction": r["direction"] or "",
                        "subject": r["subject"] or "",
                        "date": r["date_utc"] or "",
                        "body": (r["body_text"] or "")[:2000],
                        "attachments": json.loads(r["attachments_json"] or "[]"),
                    }
                    for i, r in enumerate(rows)
                ]
                out.append({
                    "thread_key": h["thread_key"],
                    "subject": messages[0]["subject"] if messages else "(no subject)",
                    "count": h["c"],
                    "messages": messages,
                })
            return out

    def thread_list(self) -> dict[str, Any]:
        """Conversation summaries for the inbox list (newest-active first) with unread
        flags, plus the total number of unread conversations."""
        with self._lock:
            heads = self._conn.execute(
                "SELECT thread_key, COUNT(*) AS c, MAX(date_utc) AS last, "
                "SUM(CASE WHEN read=0 THEN 1 ELSE 0 END) AS unread_msgs, "
                "MAX(CASE WHEN direction='inbound' THEN 1 ELSE 0 END) AS has_in, "
                "MAX(CASE WHEN direction='outbound' THEN 1 ELSE 0 END) AS has_out "
                "FROM emails GROUP BY thread_key ORDER BY last DESC"
            ).fetchall()
            threads: list[dict[str, Any]] = []
            unread_total = 0
            for h in heads:
                tk = h["thread_key"]
                root = self._conn.execute(
                    "SELECT subject FROM emails WHERE thread_key IS ? "
                    "ORDER BY date_utc ASC, id ASC LIMIT 1", (tk,)
                ).fetchone()
                last = self._conn.execute(
                    "SELECT from_name, from_address, date_utc FROM emails WHERE thread_key IS ? "
                    "ORDER BY date_utc DESC, id DESC LIMIT 1", (tk,)
                ).fetchone()
                unread = (h["unread_msgs"] or 0) > 0
                if unread:
                    unread_total += 1
                threads.append({
                    "thread_key": tk,
                    "subject": (root["subject"] if root else "") or "(no subject)",
                    "last_from": (
                        f"{last['from_name'] or ''} <{last['from_address'] or ''}>".strip()
                        if last else ""
                    ),
                    "last_date": (last["date_utc"] if last else "") or "",
                    "count": h["c"],
                    "unread": unread,
                    "has_inbound": bool(h["has_in"]),
                    "has_outbound": bool(h["has_out"]),
                })
            return {"unread_total": unread_total, "threads": threads}

    def thread(self, thread_key: str) -> dict[str, Any]:
        """One conversation's messages, ordered oldest -> newest (1st, 2nd, …)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT canonical_id,direction,subject,from_name,from_address,date_utc,"
                "body_text,attachments_json,read FROM emails WHERE thread_key IS ? "
                "ORDER BY date_utc ASC, id ASC", (thread_key,)
            ).fetchall()
        messages = [
            {
                "seq": i + 1,
                "from": f"{r['from_name'] or ''} <{r['from_address'] or ''}>".strip(),
                "direction": r["direction"] or "",
                "subject": r["subject"] or "",
                "date": r["date_utc"] or "",
                "body": r["body_text"] or "",
                "attachments": json.loads(r["attachments_json"] or "[]"),
                "read": bool(r["read"]),
            }
            for i, r in enumerate(rows)
        ]
        return {
            "thread_key": thread_key,
            "subject": messages[0]["subject"] if messages else "(no subject)",
            "count": len(messages),
            "messages": messages,
        }

    def mark_read(self, thread_key: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE emails SET read=1 WHERE thread_key IS ?", (thread_key,)
            )
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        self._conn.close()
