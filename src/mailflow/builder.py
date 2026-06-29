"""Wire a MailflowConfig into a runnable Pipeline (spec §12a). For the core spine
the only provider is `memory`, seeded by the caller."""

from __future__ import annotations

from typing import Any, Mapping

from mailflow.config.loader import validate
from mailflow.config.schema import MailflowConfig
from mailflow.core.models import StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.registry import (
    build_blob_store,
    build_cursor_store,
    build_dedupe_store,
    build_emitter,
    build_filter,
)


def build_from_config(
    cfg: MailflowConfig,
    *,
    seed: dict[StreamRef, list[SeedEmail]] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Pipeline:
    validate(cfg)  # raises on unknown kinds before we build anything
    ov = overrides or {}
    # §A10: a caller-supplied component replaces the config-built default for its role.
    if "provider" in ov:
        provider = ov["provider"]
    elif cfg.provider.kind != "memory":
        # Live providers (graph/gmail) need injected credentials + transport, so they
        # are wired via their own composition roots / run_service in
        # adapters/<kind>/live.py — not the import-light core builder.
        raise NotImplementedError(
            f"provider {cfg.provider.kind!r} runs via adapters.{cfg.provider.kind}.live."
            f"run_service (or build_{cfg.provider.kind}_runtime); build_from_config only "
            f"wires the in-memory provider"
        )
    else:
        provider = MemoryProvider(seed=seed or {})
    filters = FilterChain([build_filter(f.kind, f.params) for f in cfg.filters])
    emitter = ov["emitter"] if "emitter" in ov else build_emitter(cfg.emitter.kind)
    cursor_store = (ov["cursor_store"] if "cursor_store" in ov
                    else build_cursor_store(cfg.stores.cursor.kind, cfg.stores.cursor.params))
    dedupe_store = (ov["dedupe_store"] if "dedupe_store" in ov
                    else build_dedupe_store(cfg.stores.dedupe.kind, cfg.stores.dedupe.params))
    blob_store = (ov["blob_store"] if "blob_store" in ov
                  else build_blob_store(cfg.stores.blob.kind, cfg.stores.blob.params))
    return Pipeline(
        provider=provider,
        parser=MimeEnvelopeParser(),
        filters=filters,
        extractor=MimeExtractor(),
        emitter=emitter,
        dlq_emitter=build_emitter("memory"),
        cursor_store=cursor_store,
        dedupe_store=dedupe_store,
        blob_store=blob_store,
        cleaner=ov.get("cleaner"),
        config=PipelineConfig(
            tenant=cfg.tenant,
            max_message_bytes=cfg.max_message_bytes,
            max_attempts=cfg.max_attempts,
        ),
    )
