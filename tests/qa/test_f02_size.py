"""F02 — Size guard (unit). See docs/qa-partA-coverage.md.

The guard reads metadata (`msg.size_bytes` via `provider.message_size`) BEFORE any
extraction happens (spec §8.6/§B1). A zero/negative reported size is treated as
"unknown" and fails closed (dead-lettered), same as an oversized message — it is
never silently accepted.
"""

from __future__ import annotations

from mailflow.core.models import StreamRef
from mailflow.extract.mime import MimeExtractor
from mailflow.providers.memory import SeedEmail

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


class _SpyExtractor(MimeExtractor):
    """Records every call to `extract_bytes` so a test can assert it was never
    invoked (the size guard must reject BEFORE extraction, not after)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def extract_bytes(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().extract_bytes(*args, **kwargs)


def test_oversized_dead_letters_without_extracting(sink):
    spy = _SpyExtractor()
    seed = {S: [SeedEmail("m1", raw("m1", subject="hi", body="a body long enough"))]}
    report = build_memory_pipeline(
        seed=seed, emitter=sink, extractor=spy, max_message_bytes=10,
    ).run_once()
    assert report.dead_lettered == 1
    assert spy.calls == 0


def test_exactly_at_limit_emits(sink):
    raw_bytes = raw("m1", subject="hi", body="hello")
    seed = {S: [SeedEmail("m1", raw_bytes)]}
    report = build_memory_pipeline(
        seed=seed, emitter=sink, max_message_bytes=len(raw_bytes),
    ).run_once()
    assert report.emitted == 1


def test_one_byte_over_dead_letters(sink):
    raw_bytes = raw("m1", subject="hi", body="hello")
    seed = {S: [SeedEmail("m1", raw_bytes)]}
    report = build_memory_pipeline(
        seed=seed, emitter=sink, max_message_bytes=len(raw_bytes) - 1,
    ).run_once()
    assert report.dead_lettered == 1


def test_zero_byte_message(sink):
    # A zero-byte reported size is "unknown" and fails closed -> dead-lettered,
    # not a crash and not silently emitted (see module docstring).
    seed = {S: [SeedEmail("m1", b"")]}
    report = build_memory_pipeline(seed=seed, emitter=sink).run_once()
    assert report.dead_lettered == 1
    assert report.emitted == 0
