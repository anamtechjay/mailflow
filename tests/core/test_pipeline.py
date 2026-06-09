from datetime import datetime, timezone

import pytest

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Cursor, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import BlacklistFilter, WhitelistFilter
from mailflow.emit.memory import MemoryEmitter
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _raw(mid, frm="alice@partner.com", subject="hi", body="hello"):
    return (
        f"Message-ID: <{mid}@example.com>\r\n"
        f"From: {frm}\r\n"
        f"To: ops@acme.com\r\n"
        f"Subject: {subject}\r\n\r\n{body}\r\n"
    ).encode()


def _pipeline(seed, *, filters=None, max_message_bytes=10_000_000, max_attempts=3):
    provider = MemoryProvider(seed=seed)
    emitter = MemoryEmitter()
    dlq = MemoryEmitter()
    pipe = Pipeline(
        provider=provider,
        parser=MimeEnvelopeParser(),
        filters=FilterChain(filters or []),
        extractor=MimeExtractor(),
        emitter=emitter,
        dlq_emitter=dlq,
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(
            tenant="acme",
            max_message_bytes=max_message_bytes,
            max_attempts=max_attempts,
        ),
    )
    return pipe, emitter, dlq


def test_happy_path_emits_and_advances_cursor():
    seed = {STREAM: [SeedEmail("m1", _raw("m1")), SeedEmail("m2", _raw("m2"))]}
    pipe, emitter, _ = _pipeline(seed)
    report = pipe.run_once()
    assert report.emitted == 2
    assert [e.email.message_id for e in emitter.events] == ["<m1@example.com>", "<m2@example.com>"]
    # cursor advanced to the last message
    assert pipe.cursor_store.get("acme", STREAM) == Cursor(value=f"{STREAM.key}#2", order=2)
    # emitted event carries tenant + ordering key = mailbox
    assert emitter.events[0].tenant == "acme"
    assert emitter.events[0].ordering_key == "ops@acme.com"


def test_blacklisted_mail_is_dropped_not_emitted_and_cursor_advances():
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", frm="spammer@spam.com")),
        SeedEmail("m2", _raw("m2", frm="alice@partner.com")),
    ]}
    pipe, emitter, _ = _pipeline(seed, filters=[BlacklistFilter(domains={"spam.com"})])
    report = pipe.run_once()
    assert report.dropped == 1
    assert report.emitted == 1
    assert [e.email.provider_message_id for e in emitter.events] == ["m2"]
    # dropped message still counts as finished -> cursor advanced past BOTH
    assert pipe.cursor_store.get("acme", STREAM).order == 2
    drop_trace = next(t for t in report.traces if t.disposition.value == "dropped")
    assert drop_trace.matched_filter == "blacklist"


def test_duplicate_delivery_is_skipped_once_and_counts_as_finished():
    seed = {STREAM: [SeedEmail("m1", _raw("m1"))]}
    pipe, emitter, _ = _pipeline(seed)
    pipe.run_once()                     # first pass emits m1
    # reset the provider cursor to force a re-delivery of m1
    pipe.cursor_store = InMemoryCursorStore()
    report = pipe.run_once()            # second pass sees m1 again
    assert report.emitted == 0
    assert report.duplicates == 1       # claimed-already -> skipped
    assert len(emitter.events) == 1     # still only emitted once, ever
    assert pipe.cursor_store.get("acme", STREAM).order == 1  # duplicate advanced cursor


def test_oversized_message_is_dead_lettered_and_cursor_advances():
    big = _raw("big", body="x" * 100)
    seed = {STREAM: [SeedEmail("big", big), SeedEmail("m2", _raw("m2"))]}
    pipe, emitter, dlq = _pipeline(seed, max_message_bytes=len(big) - 1)
    report = pipe.run_once()
    assert report.dead_lettered == 1
    assert report.emitted == 1                      # m2 still flows
    assert dlq.events[0].email.provider_message_id == "big"
    assert "oversized" in report.dlq[0].reason
    assert pipe.cursor_store.get("acme", STREAM).order == 2  # moved past the poison message
