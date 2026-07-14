"""F07 — Extraction -> CleanEmail (golden + parity). See docs/qa-partA-coverage.md."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.core.models import Cursor, RawMessage, StreamRef
from mailflow.core.observability import Observers
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.email_builder import Email

TENANT = "acme"
S = StreamRef(mailbox="ops@acme.com", folder="inbox")

# Reuses the F12/F13 "poison" fixture shape: a valid MIME envelope carrying one
# attachment whose base64 payload is corrupted.
POISON_RAW = (
    b"From: a@partner.com\r\n"
    b"To: ops@acme.com\r\n"
    b"Subject: poison\r\n"
    b"Message-ID: <poison1@partner.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="BOUND"\r\n'
    b"\r\n"
    b"--BOUND\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"hello\r\n"
    b"--BOUND\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b'Content-Disposition: attachment; filename="bad.bin"\r\n'
    b"\r\n"
    b"!!!not-valid-base64!!!\r\n"
    b"--BOUND--\r\n"
)


def _gmail_clean_email(email: Email):
    return MimeExtractor().extract_bytes(
        email.as_rfc822(),
        provider="gmail",
        provider_message_id="M1",
        stream_id="ops@acme.com",
        watched_mailbox="ops@acme.com",
        blob_store=InMemoryBlobStore(),
    )


def _graph_clean_email(email: Email):
    data = email.as_graph_json()
    raw = json.dumps(data).encode()
    stream = StreamRef(mailbox="ops@acme.com", folder="inbox")
    msg = RawMessage(
        provider="graph", provider_message_id="M1", stream=stream, size_bytes=len(raw),
        received_at=datetime.now(timezone.utc), cursor=Cursor(value="1", order=1), raw_bytes=raw,
    )
    env = GraphEnvelopeParser().parse_envelope(msg, tenant="acme")
    return GraphExtractor().extract(msg, env)


def _normalized(clean_email) -> dict:
    """Subset that should agree across providers. Fields deliberately EXCLUDED (and
    why): `provider_stream_id` (the gmail call passes a bare mailbox `stream_id=`
    string, the graph call derives `StreamRef.key` which includes the folder --
    that's a call-site difference, not a parity bug); `thread_key`/`message_id_*`
    (provider-specific thread/id plumbing, not user-visible content).
    `.strip()` on body_text absorbs the stdlib email package's incidental
    trailing-newline handling of a `set_content()` text/plain part."""
    return {
        "subject": clean_email.subject,
        "from_address": clean_email.from_.address,
        "body_text": clean_email.body_text.strip(),
        "attachment_count": len(clean_email.attachments),
    }


def test_gmail_graph_parity():
    email = Email(
        msg_id="M1", subject="Parity check", body="Parity body text",
        sender="a@partner.com", to="ops@acme.com",
    )
    gmail_email = _gmail_clean_email(email)
    graph_email = _graph_clean_email(email)
    assert _normalized(gmail_email) == _normalized(graph_email)


def test_html_only_to_text():
    email = Email(
        msg_id="M2", subject="html only",
        html="<html><body><p>Hello <b>world</b></p></body></html>",
    )
    clean = _gmail_clean_email(email)
    assert clean.body_html
    assert "Hello" in clean.body_text
    assert "world" in clean.body_text
    assert "<" not in clean.body_text  # tags stripped


def test_multipart_alternative():
    email = Email(msg_id="M3", subject="alt", body="plain version", html="<p>html version</p>")
    clean = _gmail_clean_email(email)
    assert clean.body_text.strip() == "plain version"  # text/plain part chosen, not html
    assert "html version" in clean.body_html


def test_malformed_base64_permanent(sink):
    seed = {S: [SeedEmail("poison1", POISON_RAW)]}
    pipeline = Pipeline(
        provider=MemoryProvider(seed=seed),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=sink,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant=TENANT),
        observers=Observers(),
    )
    report = pipeline.run_once()
    assert report.dead_lettered == 1  # AttachmentUnreadableError (PermanentError) -> DLQ
    assert report.emitted == 0
    assert sink.events == []


def test_missing_body_empty():
    # An empty text/plain part round-trips through stdlib `set_content("")` as a
    # lone "\n" (its content-manager always terminates the payload with a
    # newline) -- `.strip()` treats that the same as "no body", the case the
    # test targets, without weakening it to accept arbitrary whitespace.
    email = Email(msg_id="M4", subject="no body", body="")
    clean = _gmail_clean_email(email)
    assert clean.body_text.strip() == ""
    assert clean.body_html == ""
    assert clean.subject == "no body"  # rest of the message still parses fine
    assert clean.attachments == []
    assert clean.canonical_id  # parsed without crashing
