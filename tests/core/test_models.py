from datetime import datetime, timezone

from mailflow.core.models import (
    Attachment,
    Cursor,
    Decision,
    Direction,
    Disposition,
    RawMessage,
    Recipient,
    StreamRef,
    Verdict,
)


def test_enum_values_are_stable_strings():
    assert Direction.inbound.value == "inbound"
    assert Disposition.dead_lettered.value == "dead_lettered"
    assert Decision.uncertain.value == "uncertain"
    assert Verdict.not_relevant.value == "not_relevant"


def test_streamref_key_with_and_without_folder():
    assert StreamRef(mailbox="ops@x.com").key == "ops@x.com"
    assert StreamRef(mailbox="ops@x.com", folder="Inbox").key == "ops@x.com:Inbox"


def test_streamref_is_hashable_and_frozen():
    s = StreamRef(mailbox="ops@x.com", folder="Inbox")
    assert s in {s}  # hashable -> usable as a dict key for per-stream state


def test_cursor_orders_by_its_order_field():
    assert Cursor(value="h2", order=2) > Cursor(value="h1", order=1)
    assert Cursor(value="h1", order=1) < Cursor(value="h2", order=2)
    assert not (Cursor(value="x", order=5) > Cursor(value="y", order=5))


def test_rawmessage_holds_cheap_metadata_plus_lazy_bytes():
    msg = RawMessage(
        provider="memory",
        provider_message_id="m1",
        stream=StreamRef(mailbox="ops@x.com", folder="Inbox"),
        size_bytes=42,
        received_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        cursor=Cursor(value="c1", order=1),
        raw_bytes=b"From: a@x.com\r\n\r\nhi",
    )
    assert msg.size_bytes == 42
    assert msg.raw_bytes.startswith(b"From:")


def test_attachment_defaults():
    att = Attachment(filename="a.pdf", content_type="application/pdf", size_bytes=10)
    assert att.is_inline is False
    assert att.content_hash == ""
    assert att.storage_ref == ""
