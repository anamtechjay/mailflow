"""Phase 1c integration — cross-lane wiring: default ContentCleaner (A6) and
the A7 thread_key chain through the pipeline (RawMessage.thread_key -> CleanEmail.thread_key).

The adapter step (Gmail threadId -> RawMessage.thread_key) is covered by
tests/test_gmail_thread_key.py; here we prove the pipeline+extractor carry it through,
and that a threadless message falls back to the normalized subject.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Iterator

from mailflow.builder import build_from_config
from mailflow.config.schema import MailflowConfig
from mailflow.core.models import Cursor, RawMessage, StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.clean import ThinContentCleaner
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


# --- A6: default ContentCleaner wired into the builder ---

def test_builder_default_cleaner_is_thin_content_cleaner() -> None:
    assert isinstance(build_from_config(MailflowConfig()).cleaner, ThinContentCleaner)


def test_builder_cleaner_is_overridable() -> None:
    sentinel = ThinContentCleaner()
    assert build_from_config(MailflowConfig(), overrides={"cleaner": sentinel}).cleaner is sentinel


# --- A7: RawMessage.thread_key -> CleanEmail.thread_key through the pipeline ---

def _raw(mid: str, subject: str) -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: a@p.com\r\n"
            f"To: ops@acme.com\r\nSubject: {subject}\r\n\r\nbody").encode()


class _OneShotProvider:
    """Yields exactly the RawMessages it is given (as a real Gmail fetch would)."""

    def __init__(self, msgs: list[RawMessage]) -> None:
        self._msgs = msgs

    def connect(self) -> None: ...
    def sync_streams(self) -> Iterable[StreamRef]:
        return [STREAM]

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        yield from self._msgs

    def message_size(self, msg: RawMessage) -> int | None:
        return msg.size_bytes


def _msg(mid: str, *, subject: str, thread_key: str) -> RawMessage:
    return RawMessage(
        provider="gmail", provider_message_id=mid, stream=STREAM, size_bytes=80,
        received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        cursor=Cursor(value="200", order=200), raw_bytes=_raw(mid, subject),
        thread_key=thread_key,
    )


def _run(msgs: list[RawMessage]) -> MemoryEmitter:
    emit = MemoryEmitter()
    Pipeline(
        provider=_OneShotProvider(msgs), parser=MimeEnvelopeParser(), filters=FilterChain([]),
        extractor=MimeExtractor(), emitter=emit, dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(), config=PipelineConfig(tenant="t"),
    ).run_once()
    return emit


def test_provider_thread_key_threads_onto_clean_email() -> None:
    emit = _run([_msg("m1", subject="Re: hello", thread_key="T-42")])
    assert emit.events[0].email.thread_key == "T-42"


def test_threadless_message_falls_back_to_normalized_subject() -> None:
    emit = _run([_msg("m1", subject="Re: Hello", thread_key="")])
    assert emit.events[0].email.thread_key == "hello"


# --- A6/B5: HTML-only mail produces non-empty body_text through the facade path ---

def test_html_only_mail_populates_body_text() -> None:
    from mailflow import connect

    html_raw = (
        b"Message-ID: <h1@x>\r\nFrom: a@p.com\r\nTo: ops@acme.com\r\n"
        b"Subject: hi\r\nContent-Type: text/html\r\n\r\n<p>Hello <b>world</b></p>"
    )
    mf = connect("memory", seed={STREAM: [SeedEmail("h1", html_raw)]}, tenant="acme")
    emails = mf.fetch_new()
    assert emails and "Hello" in emails[0].body_text
