"""Task 1: Pipeline.__init__ fails fast when an injected component does not
satisfy its @runtime_checkable port (the missing half of config validation)."""

from __future__ import annotations

import pytest

from mailflow.core.errors import ConfigError
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import FunctionFilter
from mailflow.providers.memory import MemoryProvider
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        provider=MemoryProvider(seed={}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=MimeExtractor(),
        emitter=MemoryEmitter(),
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )
    base.update(overrides)
    return base


class _NotAnEmitter:
    """No .emit method -> does not satisfy the Emitter port."""


def test_valid_in_memory_pipeline_constructs() -> None:
    Pipeline(**_kwargs())  # type: ignore[arg-type]


def test_bad_emitter_rejected_with_named_error() -> None:
    with pytest.raises(ConfigError) as exc:
        Pipeline(**_kwargs(emitter=_NotAnEmitter()))  # type: ignore[arg-type]
    msg = str(exc.value)
    assert "emitter" in msg and "Emitter" in msg


def test_bad_filter_in_chain_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        Pipeline(**_kwargs(filters=FilterChain([object()])))  # type: ignore[arg-type,list-item]
    assert "filter" in str(exc.value) and "Filter" in str(exc.value)


def test_mime_extractor_accepted_despite_no_extract_method() -> None:
    # MimeExtractor exposes extract_bytes (not the port's extract); the seam allows it.
    Pipeline(**_kwargs(extractor=MimeExtractor()))  # type: ignore[arg-type]


def test_none_classifier_and_cleaner_are_allowed() -> None:
    Pipeline(**_kwargs(classifier=None, cleaner=None))  # type: ignore[arg-type]


class _NotARefresher:
    """No .force_refresh method -> does not satisfy the AuthRefresher port."""


class _NotADeadLetterStore:
    """Missing put/list_pending/delete -> does not satisfy the DeadLetterStore port."""


def test_bad_auth_refresher_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        Pipeline(**_kwargs(auth_refresher=_NotARefresher()))  # type: ignore[arg-type]
    assert "auth_refresher" in str(exc.value) and "AuthRefresher" in str(exc.value)


def test_bad_dlq_store_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        Pipeline(**_kwargs(dlq_store=_NotADeadLetterStore()))  # type: ignore[arg-type]
    assert "dlq_store" in str(exc.value) and "DeadLetterStore" in str(exc.value)


def test_none_auth_refresher_and_dlq_store_allowed() -> None:
    Pipeline(**_kwargs(auth_refresher=None, dlq_store=None))  # type: ignore[arg-type]
