from mailflow.core.models import Cursor, StreamRef
from mailflow.providers.memory import MemoryProvider, SeedEmail

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _provider() -> MemoryProvider:
    return MemoryProvider(seed={
        STREAM: [
            SeedEmail(provider_message_id="m1", raw=b"Subject: one\r\n\r\nbody one"),
            SeedEmail(provider_message_id="m2", raw=b"Subject: two\r\n\r\nbody two"),
            SeedEmail(provider_message_id="m3", raw=b"Subject: three\r\n\r\nbody three"),
        ]
    })


def test_sync_streams_lists_seeded_streams():
    assert list(_provider().sync_streams()) == [STREAM]


def test_fetch_from_none_returns_all_in_order():
    msgs = list(_provider().fetch(STREAM, cursor=None))
    assert [m.provider_message_id for m in msgs] == ["m1", "m2", "m3"]
    assert [m.cursor.order for m in msgs] == [1, 2, 3]


def test_fetch_after_cursor_returns_only_newer():
    p = _provider()
    msgs = list(p.fetch(STREAM, cursor=Cursor(value="c2", order=2)))
    assert [m.provider_message_id for m in msgs] == ["m3"]


def test_message_size_matches_raw_length():
    p = _provider()
    msg = next(p.fetch(STREAM, cursor=None))
    assert p.message_size(msg) == len(msg.raw_bytes)


def test_unknown_stream_yields_nothing():
    assert list(_provider().fetch(StreamRef(mailbox="nope@x.com"), None)) == []
