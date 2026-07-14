"""Phase-0 harness extensions for the reliability cluster (REL/DEP/CUST/SEC/MIME/INT
reproducers). See `.../scratchpad/prod-partB-harness.md` (B1-B7) for the spec these
implement, and `.../scratchpad/prod-REL-cluster.md` for the scenarios that consume them.

Nothing here touches `src/` — every helper is a standalone test-only tool. Where a real
fix needs a `src/` seam that does not exist yet (clock injection into lease/renewal
logic), that is called out in the docstring rather than patched in.
"""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from mailflow import Mailflow, connect
from mailflow.core.models import CleanEmail, Cursor, Envelope, RawMessage, StreamRef
from mailflow.core.pipeline import Pipeline
from mailflow.core.ports import DedupeStore
from mailflow.extract.mime import MimeExtractor
from mailflow.providers.memory import SeedEmail

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

# =========================================================================== B1 batch_with_faults


class FaultByIdExtractor:
    """`ContentExtractor` that raises a chosen exception for specific
    `provider_message_id`s, delegating everything else to a real `MimeExtractor`.

    Unlike `fakes.FaultExtractor` (which fails its first N calls regardless of which
    message they belong to), this fails BY MESSAGE ID -- the tool needed to reproduce a
    mid-batch fault where message A fails and message B (later in the same `fetch()`
    batch) succeeds (REL-1 / REL-8 / CUST-4 / FIL-5)."""

    def __init__(self, fail: dict[str, BaseException], *, inner: MimeExtractor | None = None) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self._inner = inner or MimeExtractor()

    def extract(self, msg: RawMessage, env: Envelope) -> CleanEmail:
        self.calls.append(msg.provider_message_id)
        if msg.provider_message_id in self.fail:
            raise self.fail[msg.provider_message_id]
        return self._inner.extract_bytes(
            msg.raw_bytes,
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            stream_id=msg.stream.key,
            watched_mailbox=msg.stream.mailbox,
            thread_key=msg.thread_key,
        )


def batch_with_faults(
    seed: dict[StreamRef, list[SeedEmail]],
    fail: dict[str, BaseException],
    **pipeline_kwargs: Any,
) -> Pipeline:
    """Build a REAL memory `Pipeline` (via `build_memory_pipeline`) where extraction
    raises `fail[provider_message_id]` for the chosen ids within one `fetch()` batch,
    and succeeds for every other message. Drives the actual pipeline end to end (not a
    mock), so a later message's success genuinely runs `commit_if_ahead` -- the core
    tool for mid-batch cursor-skip tests (REL-1: does a later success let the cursor
    leapfrog an earlier, still-unresolved failure?)."""
    if "extractor" in pipeline_kwargs:
        raise ValueError("batch_with_faults builds its own FaultByIdExtractor; do not pass `extractor`")
    return build_memory_pipeline(seed=seed, extractor=FaultByIdExtractor(fail), **pipeline_kwargs)


# =========================================================================== B2 crash_between_claim_and_done


def crash_between_claim_and_done(store: DedupeStore, key: str, lease_seconds: int = 300) -> DedupeStore:
    """Simulate a worker crashing mid-message: `try_claim` succeeds and `record_attempt`
    runs (mirroring `Pipeline._process`'s real call order), but `mark_done` is NEVER
    called and the claim is never `release`d -- exactly what a killed process leaves
    behind. Use a PERSISTENT store (`SqliteDedupeStore`) so the stale claim is visible
    to a second store handle/pipeline built over the same backing file, modeling a real
    process restart (REL-2, DEP-7, SEC-4).

    Returns `store` (mutated in place) so the caller can wire the "restart" pipeline
    directly over it, or open a fresh store instance against the same db path to prove
    the staleness survives a real new connection.
    """
    if not store.try_claim(key, lease_seconds):
        raise AssertionError(f"crash_between_claim_and_done: key {key!r} was already claimed")
    store.record_attempt(key)
    return store


# =========================================================================== B3 FakeClock


class FakeClock:
    """Injectable monotonic clock: `now()` returns the current fake time, `advance()`
    moves it forward. The instance is itself a zero-arg `float`-returning callable, so
    it is drop-in compatible with anything typed as `Callable[[], float]` (the same
    shape as `time.monotonic`).

    NOTE (src seam Phase 1+ will need): `src/` has no injection point yet for the clock
    used by lease expiry (`DedupeStore.try_claim`/lease) or subscription renewal
    (`SubscriptionManager.renew_watch`) -- both currently rely on wall/process time
    internally (or, for the in-memory/sqlite stores, do not simulate expiry at all, see
    `stores/memory.py` and `stores/sqlite.py` docstrings). To make expiry
    deterministically testable, those components need a `clock: Callable[[], float] =
    time.monotonic` constructor parameter that this `FakeClock` can be substituted for.
    This file does NOT make that change -- it only provides the tool.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds

    def __call__(self) -> float:
        return self.now()


# =========================================================================== B4 cold_start


def cold_start(**connect_kwargs: Any) -> tuple[Mailflow, Mailflow]:
    """Build `connect(...)` TWICE, forcing `state="memory"` on both calls -- each call
    gets fresh, empty cursor/dedupe state, with no shared file/db between them, exactly
    like two separate serverless invocations with no warm container (DEP-1, DEP-3).
    Any `state=` passed in `connect_kwargs` is overridden (a cold start is, by
    definition, never restart-safe state). Returns both handles so the caller can drive
    them independently and observe what a cold restart loses/duplicates."""
    kwargs = dict(connect_kwargs)
    kwargs["state"] = "memory"
    return connect(**kwargs), connect(**kwargs)


# =========================================================================== B5 MIME_ADVERSARIAL

_BOUNDARY_C = "BOUNDARY-C-adversarial"
_BOUNDARY_D = "BOUNDARY-D-adversarial"
_BOUNDARY_E1 = "BOUNDARY-E1-adversarial"
_BOUNDARY_E2 = "BOUNDARY-E2-adversarial"
_BOUNDARY_E3 = "BOUNDARY-E3-adversarial"

_UNKNOWN_CHARSET = (
    b"From: a@partner.com\r\n"
    b"To: me@acme.com\r\n"
    b"Subject: unknown charset\r\n"
    b"Message-ID: <MIME-UNKNOWN-CHARSET@partner.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=x-totally-made-up\r\n"
    b"Content-Transfer-Encoding: 7bit\r\n"
    b"\r\n"
    b"Hello from a charset nobody registered.\r\n"
)

_INLINE_DISPOSITION_TEXT = (
    b"From: a@partner.com\r\n"
    b"To: me@acme.com\r\n"
    b"Subject: inline disposition body\r\n"
    b"Message-ID: <MIME-INLINE-DISPOSITION@partner.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=us-ascii\r\n"
    b"Content-Disposition: inline\r\n"
    b"\r\n"
    b"Body sent with Content-Disposition: inline (mutt/Mailman style).\r\n"
)

_FORWARDED_MESSAGE_RFC822 = (
    b"From: a@partner.com\r\n"
    b"To: me@acme.com\r\n"
    b"Subject: Fwd: original\r\n"
    b"Message-ID: <MIME-FORWARDED@partner.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="' + _BOUNDARY_C.encode() + b'"\r\n'
    b"\r\n"
    b"--" + _BOUNDARY_C.encode() + b"\r\n"
    b"Content-Type: text/plain; charset=us-ascii\r\n"
    b"\r\n"
    b"See forwarded message below.\r\n"
    b"\r\n"
    b"--" + _BOUNDARY_C.encode() + b"\r\n"
    b"Content-Type: message/rfc822\r\n"
    b'Content-Disposition: attachment; filename="forwarded.eml"\r\n'
    b"\r\n"
    b"From: original-sender@example.com\r\n"
    b"To: someone@example.com\r\n"
    b"Subject: Original message\r\n"
    b"Message-ID: <MIME-FORWARDED-ORIGINAL@example.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: text/plain; charset=us-ascii\r\n"
    b"\r\n"
    b"This is the original forwarded content.\r\n"
    b"\r\n"
    b"--" + _BOUNDARY_C.encode() + b"--\r\n"
)

_HOSTILE_ATTACHMENT_FILENAME = (
    b"From: a@partner.com\r\n"
    b"To: me@acme.com\r\n"
    b"Subject: hostile filename\r\n"
    b"Message-ID: <MIME-HOSTILE-FILENAME@partner.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="' + _BOUNDARY_D.encode() + b'"\r\n'
    b"\r\n"
    b"--" + _BOUNDARY_D.encode() + b"\r\n"
    b"Content-Type: text/plain; charset=us-ascii\r\n"
    b"\r\n"
    b"See attached.\r\n"
    b"\r\n"
    b"--" + _BOUNDARY_D.encode() + b"\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b'Content-Disposition: attachment; filename="../../etc/passwd"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"aGVsbG8gd29ybGQ=\r\n"
    b"\r\n"
    b"--" + _BOUNDARY_D.encode() + b"--\r\n"
)

_DEEPLY_NESTED_MULTIPART = (
    b"From: a@partner.com\r\n"
    b"To: me@acme.com\r\n"
    b"Subject: deeply nested multipart\r\n"
    b"Message-ID: <MIME-DEEP-NEST@partner.com>\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="' + _BOUNDARY_E1.encode() + b'"\r\n'
    b"\r\n"
    b"--" + _BOUNDARY_E1.encode() + b"\r\n"
    b'Content-Type: multipart/alternative; boundary="' + _BOUNDARY_E2.encode() + b'"\r\n'
    b"\r\n"
    b"--" + _BOUNDARY_E2.encode() + b"\r\n"
    b"Content-Type: text/plain; charset=us-ascii\r\n"
    b"\r\n"
    b"plain fallback, outside the innermost nesting\r\n"
    b"\r\n"
    b"--" + _BOUNDARY_E2.encode() + b"\r\n"
    b'Content-Type: multipart/related; boundary="' + _BOUNDARY_E3.encode() + b'"\r\n'
    b"\r\n"
    b"--" + _BOUNDARY_E3.encode() + b"\r\n"
    b"Content-Type: text/html; charset=us-ascii\r\n"
    b"\r\n"
    b"<html><body>deepest part, four multiparts down</body></html>\r\n"
    b"\r\n"
    b"--" + _BOUNDARY_E3.encode() + b"--\r\n"
    b"\r\n"
    b"--" + _BOUNDARY_E2.encode() + b"--\r\n"
    b"\r\n"
    b"--" + _BOUNDARY_E1.encode() + b"--\r\n"
)

MIME_ADVERSARIAL: list[dict[str, Any]] = [
    {"name": "unknown_charset", "raw": _UNKNOWN_CHARSET},
    {"name": "inline_disposition_text", "raw": _INLINE_DISPOSITION_TEXT},
    {"name": "forwarded_message_rfc822", "raw": _FORWARDED_MESSAGE_RFC822},
    {"name": "hostile_attachment_filename", "raw": _HOSTILE_ATTACHMENT_FILENAME},
    {"name": "deeply_nested_multipart", "raw": _DEEPLY_NESTED_MULTIPART},
]


# =========================================================================== B6 StubProvider


class StubProvider:
    """Minimal, from-scratch `MailboxProvider` implementation -- deliberately NOT built
    on top of `MemoryProvider` -- proving the BYO-provider path (a consumer's own class,
    satisfying the port structurally, can drive `Pipeline` directly)."""

    PROVIDER = "stub"

    def __init__(self, seed: dict[StreamRef, list[SeedEmail]] | None = None) -> None:
        self._seed = seed or {}
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def sync_streams(self) -> Iterable[StreamRef]:
        return list(self._seed.keys())

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        start = cursor.order if cursor is not None else 0
        for index, item in enumerate(self._seed.get(stream, []), start=1):
            if index <= start:
                continue
            yield RawMessage(
                provider=self.PROVIDER,
                provider_message_id=item.provider_message_id,
                stream=stream,
                size_bytes=len(item.raw),
                received_at=item.received_at or datetime(2026, 7, 1, tzinfo=timezone.utc),
                cursor=Cursor(value=f"{stream.key}#{index}", order=index),
                raw_bytes=item.raw,
            )

    def message_size(self, msg: RawMessage) -> int | None:
        return msg.size_bytes


class StubProviderRaising:
    """A `MailboxProvider` whose `fetch()` raises a native (non-mailflow) error, such as
    `socket.timeout`, the instant it is called -- BEFORE yielding anything. Models a real
    network/library failure surfacing straight out of a BYO provider, unwrapped, so a
    test can characterize whether `Pipeline.run_once()` propagates it (CUST-1/2/4/5)."""

    PROVIDER = "stub-raising"

    def __init__(self, error: BaseException | None = None, *, mailbox: str = "raising@acme.com") -> None:
        self.error = error if error is not None else socket.timeout("stub socket timeout")
        self.mailbox = mailbox
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def sync_streams(self) -> Iterable[StreamRef]:
        return [StreamRef(mailbox=self.mailbox, folder="inbox")]

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        raise self.error

    def message_size(self, msg: RawMessage) -> int | None:
        return msg.size_bytes


# =========================================================================== B7 two_tenant_seed / soak_seed


def two_tenant_seed(
    n: int = 3,
    *,
    mailbox: str = "shared@acme.com",
    folder: str = "inbox",
    tenant_a: str = "tenant-a",
    tenant_b: str = "tenant-b",
) -> tuple[str, str, StreamRef, dict[StreamRef, list[SeedEmail]]]:
    """Two tenant labels pointed at the SAME stream with the SAME `provider_message_id`s
    ("SHARED0".."SHARED{n-1}") -- overlapping message ids across tenants. Returns
    `(tenant_a, tenant_b, stream, seed)`; build one `Pipeline` per tenant (differing only
    in `PipelineConfig.tenant`) over this identical seed to prove `idempotency_key`'s
    tenant component keeps the two tenants' dedupe/cursor state from colliding (INT-2)."""
    stream = StreamRef(mailbox=mailbox, folder=folder)
    seed = {
        stream: [
            SeedEmail(f"SHARED{i}", raw(f"SHARED{i}", subject=f"shared {i}"))
            for i in range(n)
        ]
    }
    return tenant_a, tenant_b, stream, seed


def soak_seed(
    n: int,
    clock: FakeClock,
    *,
    stream: StreamRef | None = None,
    step_seconds: float = 1.0,
) -> dict[StreamRef, list[SeedEmail]]:
    """`n` distinct messages ("SOAK0".."SOAK{n-1}") on one stream, each timestamped from
    `clock.now()` and advancing `clock` by `step_seconds` between messages -- a
    deterministic, monotonically-increasing-receipt-time driver for a long-run soak test
    (INT-7), parameterized by the injectable `FakeClock` (B3) rather than wall time."""
    target_stream = stream or StreamRef(mailbox="soak@acme.com", folder="inbox")
    items = []
    for i in range(n):
        received_at = datetime.fromtimestamp(clock.now(), tz=timezone.utc)
        items.append(SeedEmail(f"SOAK{i}", raw(f"SOAK{i}", subject=f"soak {i}"), received_at=received_at))
        clock.advance(step_seconds)
    return {target_stream: items}


__all__ = [
    "FaultByIdExtractor",
    "batch_with_faults",
    "crash_between_claim_and_done",
    "FakeClock",
    "cold_start",
    "MIME_ADVERSARIAL",
    "StubProvider",
    "StubProviderRaising",
    "two_tenant_seed",
    "soak_seed",
]
