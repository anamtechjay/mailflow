"""SqliteEmailStore — the consuming-app side store for received CleanEmails.

This is the store the inbox UI + scripts/{inbox_app,run_gmail_to_inbox,query_emails}
depend on. The core ingestion suite never exercises it (it lives on the consumer side),
so these are its first unit tests: save/dedupe, recent ordering, thread grouping (by
References root, with a canonical_id fallback), unread accounting, and restart-safety.
"""

from __future__ import annotations

from typing import Any

import pytest

from mailflow.persistence import SqliteEmailStore


def _event(
    cid: str,
    *,
    subject: str = "Hi",
    from_addr: str = "alice@partner.com",
    from_name: str = "Alice",
    to: tuple[str, ...] = ("ops@acme.com",),
    cc: tuple[str, ...] = (),
    references: tuple[str, ...] = (),
    direction: str = "inbound",
    date_utc: str = "2026-06-30T10:00:00Z",
    attachments: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """An EmailEvent dict shaped exactly like what the pipeline publishes to the queue
    (top-level tenant + nested `email`)."""
    return {
        "tenant": "acme",
        "email": {
            "canonical_id": cid,
            "provider": "gmail",
            "provider_message_id": f"{cid}-pmid",
            "provider_stream_id": "ops@acme.com",
            "direction": direction,
            "subject": subject,
            "from": {"name": from_name, "address": from_addr},
            "to": [{"address": a} for a in to],
            "cc": [{"address": a} for a in cc],
            "date_utc": date_utc,
            "received_at": date_utc,
            "body_text": f"body of {cid}",
            "body_html": "",
            "attachments": list(attachments),
            "references": list(references),
            "message_size_bytes": 1234,
            "schema_version": "1.3",
        },
    }


@pytest.fixture
def store(tmp_path: Any) -> Any:
    s = SqliteEmailStore(str(tmp_path / "emails.db"))
    yield s
    s.close()


# ---- save / dedupe / count ----

def test_save_new_returns_true_and_counts(store: SqliteEmailStore) -> None:
    assert store.save(_event("m1")) is True
    assert store.count() == 1


def test_save_duplicate_canonical_id_is_ignored(store: SqliteEmailStore) -> None:
    assert store.save(_event("m1", subject="first")) is True
    # same canonical_id -> INSERT OR IGNORE -> no new row, returns False
    assert store.save(_event("m1", subject="second")) is False
    assert store.count() == 1


def test_distinct_ids_each_stored(store: SqliteEmailStore) -> None:
    for cid in ("m1", "m2", "m3"):
        assert store.save(_event(cid)) is True
    assert store.count() == 3


# ---- recent ----

def test_recent_returns_newest_first(store: SqliteEmailStore) -> None:
    store.save(_event("m1", subject="oldest"))
    store.save(_event("m2", subject="middle"))
    store.save(_event("m3", subject="newest"))
    rows = store.recent()
    assert [r["subject"] for r in rows] == ["newest", "middle", "oldest"]
    # the projected columns the inbox list relies on
    assert rows[0]["canonical_id"] == "m3"
    assert rows[0]["from_address"] == "alice@partner.com"
    assert rows[0]["direction"] == "inbound"


def test_recent_respects_limit(store: SqliteEmailStore) -> None:
    for i in range(5):
        store.save(_event(f"m{i}"))
    assert len(store.recent(limit=2)) == 2


def test_recent_attachment_count_is_projected(store: SqliteEmailStore) -> None:
    store.save(_event("m1", attachments=({"filename": "a.pdf"}, {"filename": "b.png"})))
    assert store.recent()[0]["attachment_count"] == 2


# ---- thread grouping ----

def test_thread_grouped_by_references_root(store: SqliteEmailStore) -> None:
    # m2 replies to m1 (its References root is m1's id) -> same conversation
    store.save(_event("m1", subject="Q", date_utc="2026-06-30T10:00:00Z"))
    store.save(_event("m2", subject="Re: Q", references=("m1",),
                      date_utc="2026-06-30T11:00:00Z"))
    threads = store.threads()
    assert len(threads) == 1
    t = threads[0]
    assert t["count"] == 2
    # ordered oldest -> newest with 1-based seq
    assert [m["seq"] for m in t["messages"]] == [1, 2]
    assert [m["subject"] for m in t["messages"]] == ["Q", "Re: Q"]


def test_thread_key_falls_back_to_canonical_id(store: SqliteEmailStore) -> None:
    # no References -> the message is its own thread root
    store.save(_event("solo", references=()))
    listing = store.thread_list()
    assert len(listing["threads"]) == 1
    assert listing["threads"][0]["thread_key"] == "solo"


# ---- thread_list: unread accounting + direction flags ----

def test_thread_list_unread_total_and_mark_read(store: SqliteEmailStore) -> None:
    store.save(_event("m1", date_utc="2026-06-30T10:00:00Z"))
    store.save(_event("m2", references=("m1",), date_utc="2026-06-30T11:00:00Z"))

    listing = store.thread_list()
    assert listing["unread_total"] == 1
    assert listing["threads"][0]["unread"] is True
    assert listing["threads"][0]["count"] == 2

    # mark the whole conversation read -> both rows updated
    assert store.mark_read("m1") == 2

    after = store.thread_list()
    assert after["unread_total"] == 0
    assert after["threads"][0]["unread"] is False


def test_thread_list_inbound_outbound_flags(store: SqliteEmailStore) -> None:
    store.save(_event("in", direction="inbound", date_utc="2026-06-30T10:00:00Z"))
    store.save(_event("out", direction="outbound", references=("in",),
                      date_utc="2026-06-30T11:00:00Z"))
    head = store.thread_list()["threads"][0]
    assert head["has_inbound"] is True
    assert head["has_outbound"] is True


# ---- thread(key): full conversation with read flags ----

def test_thread_returns_messages_oldest_first_with_read_flags(store: SqliteEmailStore) -> None:
    store.save(_event("m1", subject="Q", date_utc="2026-06-30T10:00:00Z",
                      attachments=({"filename": "spec.pdf"},)))
    store.save(_event("m2", subject="Re: Q", references=("m1",),
                      date_utc="2026-06-30T11:00:00Z"))
    convo = store.thread("m1")
    assert convo["thread_key"] == "m1"
    assert convo["count"] == 2
    assert convo["subject"] == "Q"
    assert [m["seq"] for m in convo["messages"]] == [1, 2]
    assert convo["messages"][0]["attachments"] == [{"filename": "spec.pdf"}]
    assert all(m["read"] is False for m in convo["messages"])

    store.mark_read("m1")
    assert all(m["read"] is True for m in store.thread("m1")["messages"])


def test_thread_unknown_key_is_empty(store: SqliteEmailStore) -> None:
    convo = store.thread("nope")
    assert convo["count"] == 0
    assert convo["messages"] == []
    assert convo["subject"] == "(no subject)"


def test_mark_read_unknown_key_returns_zero(store: SqliteEmailStore) -> None:
    store.save(_event("m1"))
    assert store.mark_read("does-not-exist") == 0


# ---- restart-safety (a fresh store on the same file sees prior rows) ----

def test_persists_across_reopen(tmp_path: Any) -> None:
    path = str(tmp_path / "emails.db")
    s1 = SqliteEmailStore(path)
    s1.save(_event("m1"))
    s1.save(_event("m2", references=("m1",)))
    s1.close()

    s2 = SqliteEmailStore(path)
    try:
        assert s2.count() == 2
        # the conversation survived the restart
        assert s2.thread_list()["threads"][0]["count"] == 2
    finally:
        s2.close()
