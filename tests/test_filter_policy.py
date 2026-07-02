from mailflow.core.models import CleanEmail


def test_cleanemail_disposition_defaults_to_emitted():
    email = CleanEmail(canonical_id="c1", provider="memory", provider_message_id="m1",
                       provider_stream_id="s1")
    assert email.disposition == "emitted"
    assert email.filter_reason == ""


def test_cleanemail_can_be_marked_filtered():
    email = CleanEmail(canonical_id="c1", provider="memory", provider_message_id="m1",
                       provider_stream_id="s1")
    email.disposition = "filtered"
    email.filter_reason = "list/auto-submitted header present"
    assert email.disposition == "filtered"
    assert email.filter_reason == "list/auto-submitted header present"


import pytest

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Disposition
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import ListMailFilter
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.core.models import StreamRef
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.clean import ThinContentCleaner
from mailflow.stores.memory import (
    InMemoryCursorStore, InMemoryDedupeStore, InMemoryBlobStore,
)


AUTO_SUBMITTED_RAW = (
    b"From: mailer@example.com\r\n"
    b"To: me@example.com\r\n"
    b"Subject: Out of office\r\n"
    b"Auto-Submitted: auto-replied\r\n"
    b"Message-ID: <a1@example.com>\r\n\r\n"
    b"I am away.\r\n"
)


def _pipeline(on_filtered):
    stream = StreamRef(mailbox="me@example.com", folder=None)
    provider = MemoryProvider(seed={stream: [SeedEmail(provider_message_id="m1",
                                                        raw=AUTO_SUBMITTED_RAW)]})
    emitter = MemoryEmitter()
    return provider, emitter, Pipeline(
        provider=provider,
        parser=MimeEnvelopeParser(),
        filters=FilterChain([ListMailFilter()]),
        extractor=MimeExtractor(),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        cleaner=ThinContentCleaner(),
        config=PipelineConfig(tenant="acme", on_filtered=on_filtered),
    )


def test_drop_mode_suppresses_filtered_email():
    _provider, emitter, pipe = _pipeline("drop")
    report = pipe.run_once()
    assert report.dropped == 1
    assert report.emitted == 0
    assert emitter.events == []


def test_tag_mode_delivers_filtered_email_with_tag():
    _provider, emitter, pipe = _pipeline("tag")
    report = pipe.run_once()
    assert report.emitted == 1
    assert report.dropped == 0
    email = emitter.events[0].email
    assert email.disposition == "filtered"
    assert email.matched_filter  # the filter name is set
    assert email.filter_reason   # a human reason is set


def test_tag_mode_is_the_default():
    stream = StreamRef(mailbox="me@example.com", folder=None)
    cfg = PipelineConfig(tenant="acme")
    assert cfg.on_filtered == "tag"


def test_emitted_email_disposition_stays_emitted():
    # a message that passes all filters keeps disposition == "emitted"
    stream = StreamRef(mailbox="me@example.com", folder=None)
    normal = (b"From: a@example.com\r\nTo: me@example.com\r\n"
              b"Subject: hi\r\nMessage-ID: <n1@example.com>\r\n\r\nhello\r\n")
    provider = MemoryProvider(seed={stream: [SeedEmail(provider_message_id="n1", raw=normal)]})
    emitter = MemoryEmitter()
    pipe = Pipeline(
        provider=provider, parser=MimeEnvelopeParser(),
        filters=FilterChain([ListMailFilter()]), extractor=MimeExtractor(),
        emitter=emitter, dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), cleaner=ThinContentCleaner(),
        config=PipelineConfig(tenant="acme", on_filtered="tag"),
    )
    pipe.run_once()
    assert emitter.events[0].email.disposition == "emitted"
