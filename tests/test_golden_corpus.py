"""Golden-master: 17 real emails -> CleanEmail, compared to hand-authored output."""

from __future__ import annotations

from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.emit.memory import MemoryEmitter
from mailflow.providers.memory import MemoryProvider
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)
from tests.fixtures.loader import STREAM, load_golden, seed

GOLDEN_FIELDS = (
    "canonical_id", "message_id", "message_id_present", "message_id_trusted", "in_reply_to",
    "references", "direction", "from", "to", "cc", "subject",
    "attachments", "list_id", "auto_submitted",
)


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in (text or "").splitlines()).strip()


def _project(dumped: dict) -> dict:
    return {k: dumped[k] for k in GOLDEN_FIELDS}


def _run() -> MemoryEmitter:
    emitter = MemoryEmitter()
    pipe = Pipeline(
        provider=MemoryProvider(seed=seed()),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme", max_message_bytes=10_000_000),
    )
    report = pipe.run_once()
    assert report.emitted == 17
    assert report.dropped == 0 and report.dead_lettered == 0
    return emitter


def test_every_real_email_matches_its_golden_clean_email():
    golden = load_golden()
    emitter = _run()
    assert len(emitter.events) == 17
    for event in emitter.events:
        dumped = event.email.model_dump(by_alias=True)
        mid = dumped["message_id"]
        assert mid in golden, f"unexpected message {mid}"
        expected = golden[mid]
        # exact match on identity / threading / routing fields
        assert _project(dumped) == _project(expected), f"field mismatch for {mid}"
        # body compared with newline/whitespace normalization (round-trip tolerant)
        assert _norm(dumped["body_text"]) == _norm(expected["body_text"]), f"body mismatch for {mid}"


def test_deep_reference_chain_is_carried_verbatim():
    golden = load_golden()
    emitter = _run()
    by_mid = {e.email.message_id: e.email for e in emitter.events}
    # the last customer message references the whole 16-deep chain
    deepest = max(golden.values(), key=lambda g: len(g["references"]))
    assert len(deepest["references"]) == 16
    assert by_mid[deepest["message_id"]].references == deepest["references"]


def test_direction_split_matches_watched_mailbox():
    emitter = _run()
    outbound = [e for e in emitter.events if e.email.direction.value == "outbound"]
    inbound = [e for e in emitter.events if e.email.direction.value == "inbound"]
    assert len(outbound) == 7   # from testuser1@cowboyslogistics.com
    assert len(inbound) == 10   # from sandyqatestacc@yahoo.com
