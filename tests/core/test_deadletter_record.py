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
