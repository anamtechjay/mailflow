"""Scenario — duplicate delivery. See docs/qa-partA-coverage.md ("Scenarios & invariants").

`test_same_id_x5_emits_once` feeds the identical provider_message_id 5 times in
one stream/run: only the first claim wins, the other 4 are counted as
duplicates and nothing extra is delivered. `test_interleaved_two_streams_independent`
proves that two mailboxes carrying the SAME provider_message_id don't dedupe
against each other (the idempotency key includes the mailbox — see F01's
`test_same_msg_two_mailboxes_emits_twice`), while duplicates *within* each
stream are still suppressed independently.
"""

from __future__ import annotations

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

from tests._harness.email_builder import raw
from tests._harness.fakes import build_memory_pipeline

S = StreamRef(mailbox="ops@acme.com", folder="inbox")
S2 = StreamRef(mailbox="ops2@acme.com", folder="inbox")


def test_same_id_x5_emits_once(sink):
    # Five distinct provider "deliveries" of the same message id on one stream:
    # each gets its own cursor position (list index), but they all resolve to
    # the same idempotency_key, so only the first is claimed/emitted.
    seed = {S: [SeedEmail("dup1", raw("dup1")) for _ in range(5)]}
    report = build_memory_pipeline(seed=seed, emitter=sink).run_once()

    assert report.fetched == 5
    assert report.emitted == 1
    assert report.duplicates == 4
    assert len(sink.events) == 1


def test_interleaved_two_streams_independent(sink):
    # "shared1" appears on BOTH mailboxes -- different idempotency_key (mailbox
    # is part of the key) so each mailbox's copy is its own, independent arrival.
    # Each stream also carries one intra-stream duplicate, proving per-stream
    # dedupe doesn't leak across streams either way.
    seed = {
        S: [SeedEmail("shared1", raw("shared1")), SeedEmail("m2", raw("m2")),
            SeedEmail("shared1", raw("shared1"))],
        S2: [SeedEmail("shared1", raw("shared1")), SeedEmail("m3", raw("m3")),
             SeedEmail("m3", raw("m3"))],
    }
    report = build_memory_pipeline(seed=seed, emitter=sink).run_once()

    assert report.fetched == 6
    assert report.emitted == 4          # S: {shared1, m2}; S2: {shared1, m3}
    assert report.duplicates == 2        # one repeat in each stream
    assert len(sink.events) == 4

    emitted_keys = {(event.email.provider_stream_id, event.email.provider_message_id)
                    for event in sink.events}
    assert emitted_keys == {
        (S.key, "shared1"), (S.key, "m2"),
        (S2.key, "shared1"), (S2.key, "m3"),
    }
