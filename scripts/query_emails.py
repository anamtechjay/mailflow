"""Show the emails stored in the SQLite DB — proves persistence. Run anytime:
  python scripts/query_emails.py
Reads env: EMAIL_DB (default 'emails.db').
"""

from __future__ import annotations

import os

from mailflow.persistence import SqliteEmailStore


def main() -> None:
    store = SqliteEmailStore(os.environ.get("EMAIL_DB", "emails.db"))
    rows = store.recent(50)
    print(f"Total stored: {store.count()}   (db = {store.db_path})\n")
    if not rows:
        print("No emails stored yet — run the dashboard/consumer and send some.")
        return
    for r in rows:
        print(f"#{r['id']:<4} [{(r['direction'] or ''):<8}] "
              f"{(r['from_address'] or ''):<34} {r['subject']!r}  "
              f"att={r['attachment_count']}  stored={r['stored_at']}")


if __name__ == "__main__":
    main()
