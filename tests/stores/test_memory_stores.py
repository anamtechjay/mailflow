from mailflow.core.models import Cursor, StreamRef
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def test_cursor_starts_empty():
    assert InMemoryCursorStore().get("acme", STREAM) is None


def test_cursor_commit_if_ahead_only_advances_forward():
    cs = InMemoryCursorStore()
    assert cs.commit_if_ahead("acme", STREAM, Cursor(value="a", order=1)) is True
    assert cs.commit_if_ahead("acme", STREAM, Cursor(value="b", order=2)) is True
    # an older cursor is rejected (monotonic, spec §8.3)
    assert cs.commit_if_ahead("acme", STREAM, Cursor(value="stale", order=1)) is False
    assert cs.get("acme", STREAM) == Cursor(value="b", order=2)


def test_cursor_is_namespaced_per_tenant_and_stream():
    cs = InMemoryCursorStore()
    cs.commit_if_ahead("acme", STREAM, Cursor(value="a", order=5))
    assert cs.get("other", STREAM) is None
    assert cs.get("acme", StreamRef(mailbox="ops@acme.com")) is None  # folderless = different stream


def test_dedupe_claim_is_exclusive():
    ds = InMemoryDedupeStore()
    assert ds.try_claim("k1", lease_seconds=60) is True
    assert ds.try_claim("k1", lease_seconds=60) is False  # already claimed


def test_dedupe_release_allows_reclaim():
    ds = InMemoryDedupeStore()
    ds.try_claim("k1", 60)
    ds.release("k1")
    assert ds.try_claim("k1", 60) is True


def test_dedupe_mark_done_blocks_future_claims():
    ds = InMemoryDedupeStore()
    ds.try_claim("k1", 60)
    ds.mark_done("k1", ttl_seconds=3600)
    assert ds.try_claim("k1", 60) is False  # done stays claimed for the TTL window


def test_dedupe_records_attempts():
    ds = InMemoryDedupeStore()
    ds.try_claim("k1", 60)
    assert ds.record_attempt("k1") == 1
    assert ds.record_attempt("k1") == 2


def test_blob_put_and_open_round_trip():
    bs = InMemoryBlobStore()
    ref = bs.put_stream("acme/att1", iter([b"PDF", b"BYTES"]), "application/pdf")
    assert b"".join(bs.open(ref)) == b"PDFBYTES"
