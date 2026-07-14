"""INT-2 — two tenants processed in one host, sharing one dedupe store, must not collide.
The idempotency key is (tenant, mailbox, provider_message_id), so the SAME mailbox +
message id under two different tenants are two independent messages: both emit, neither
is mistaken for the other's duplicate, and one tenant's claims never suppress another's.
"""

from __future__ import annotations

import pytest

from mailflow.core.observability import Observers
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore

from tests._harness.reliability import two_tenant_seed

pytestmark = pytest.mark.reliability


def _pipe(tenant, seed, emitter, dedupe):
    return Pipeline(
        provider=MemoryProvider(seed=seed),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),   # cursor is per (tenant, stream) — independent
        dedupe_store=dedupe,                   # SHARED across both tenants on purpose
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant=tenant),
        observers=Observers(),
    )


def test_two_tenants_same_mailbox_and_id_do_not_collide():
    tenant_a, tenant_b, _stream, seed = two_tenant_seed(3)
    shared_dedupe = InMemoryDedupeStore()
    sink_a, sink_b = MemoryEmitter(), MemoryEmitter()

    report_a = _pipe(tenant_a, seed, sink_a, shared_dedupe).run_once()
    report_b = _pipe(tenant_b, seed, sink_b, shared_dedupe).run_once()

    # Both tenants deliver all 3 — tenant B's identical (mailbox, id) tuples are NOT
    # swallowed as duplicates of tenant A's, because the tenant is part of the key.
    assert report_a.emitted == 3
    assert report_b.emitted == 3
    assert report_b.duplicates == 0
    assert len(sink_a.events) == 3 and len(sink_b.events) == 3
