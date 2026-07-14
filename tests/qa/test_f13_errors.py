"""F13 — Error ladder (unit + fault-inject). See docs/qa-partA-coverage.md.

FIXED (REL-3 / finding I5+P1, see tests/qa/test_rel3_retry_exhaustion.py and
`src/mailflow/stores/memory.py::InMemoryDedupeStore`'s docstring): `release()`
used to `del` the whole claim record, so `attempts` reset to 0 on every
redelivery and the retry-exhaustion path was unreachable via repeated
`run_once()` runs against this store -- a persistently-failing message would
retry forever and never DLQ. `release()` now only clears the `claimed` flag and
keeps the record (`attempts` is a lifetime counter for the key), so exhaustion
IS reachable across separate runs with the default `max_attempts=3`.

The "exhausts" test below still configures `max_attempts=1` (via a hand-built
Pipeline; `build_memory_pipeline` does not expose this knob) to keep exercising
exhaustion on the very FIRST attempt within a single run -- a cheap, deterministic
unit check. The realistic multi-run/default-max_attempts scenario lives in
`test_rel3_retry_exhaustion.py`.
"""

from __future__ import annotations

from mailflow.core.errors import AuthError
from mailflow.core.models import CleanEmail, Envelope, RawMessage, StreamRef
from mailflow.core.observability import Observers
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.email_builder import raw
from tests._harness.fakes import FaultExtractor, build_memory_pipeline

TENANT = "acme"
S = StreamRef(mailbox="ops@acme.com", folder="inbox")

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


class _AuthFaultExtractor:
    """Raises AuthError for its first `fail_until` calls, then delegates to a real
    MimeExtractor -- the AuthError analogue of tests/_harness/fakes.FaultExtractor."""

    def __init__(self, fail_until: int) -> None:
        self.fail_until = fail_until
        self.calls = 0
        self._inner = MimeExtractor()

    def extract(self, msg: RawMessage, env: Envelope) -> CleanEmail:
        self.calls += 1
        if self.calls <= self.fail_until:
            raise AuthError(f"synthetic auth failure {self.calls}/{self.fail_until}")
        return self._inner.extract_bytes(
            msg.raw_bytes, provider=msg.provider, provider_message_id=msg.provider_message_id,
            stream_id=msg.stream.key, watched_mailbox=msg.stream.mailbox,
        )


class _SpyAuthRefresher:
    def __init__(self) -> None:
        self.calls = 0

    def force_refresh(self) -> None:
        self.calls += 1


def test_transient_then_success(sink):
    fx = FaultExtractor(fail_until=1)
    stores = dict(cursor_store=InMemoryCursorStore(), dedupe_store=InMemoryDedupeStore())
    seed = {S: [SeedEmail("m1", raw("m1"))]}

    r1 = build_memory_pipeline(seed=seed, emitter=sink, extractor=fx, stores=stores).run_once()
    assert r1.emitted == 0
    assert stores["cursor_store"].get(TENANT, S) is None  # not terminal -> unmoved

    r2 = build_memory_pipeline(seed=seed, emitter=sink, extractor=fx, stores=stores).run_once()
    assert r2.emitted == 1
    assert stores["cursor_store"].get(TENANT, S) is not None
    assert stores["cursor_store"].get(TENANT, S).order == 1


def test_transient_exhausts_to_dlq(sink):
    # See module docstring: max_attempts=1 makes exhaustion reachable on attempt 1.
    fx = FaultExtractor(fail_until=99)
    pipeline = Pipeline(
        provider=MemoryProvider(seed={S: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=fx,
        emitter=sink,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant=TENANT, max_attempts=1),
        observers=Observers(),
    )
    report = pipeline.run_once()
    assert report.dead_lettered == 1
    assert report.emitted == 0


def test_permanent_to_dlq(sink):
    # Malformed base64 attachment -> AttachmentUnreadableError (PermanentError) ->
    # straight to DLQ on the FIRST attempt, no retry/release involved.
    seed = {S: [SeedEmail("poison1", POISON_RAW)]}
    report = build_memory_pipeline(seed=seed, emitter=sink).run_once()
    assert report.dead_lettered == 1
    assert report.emitted == 0


def test_auth_refresh_retry_once(sink):
    fx = _AuthFaultExtractor(fail_until=1)
    refresher = _SpyAuthRefresher()
    pipeline = Pipeline(
        provider=MemoryProvider(seed={S: [SeedEmail("m1", raw("m1"))]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=fx,  # type: ignore[arg-type]
        emitter=sink,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant=TENANT),
        auth_refresher=refresher,
        observers=Observers(),
    )
    report = pipeline.run_once()
    assert refresher.calls == 1  # exactly one forced refresh
    assert report.emitted == 1  # then the in-process retry succeeds
    assert fx.calls == 2
