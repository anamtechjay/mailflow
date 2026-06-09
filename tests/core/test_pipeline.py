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
