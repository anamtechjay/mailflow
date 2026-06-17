"""Built-in kind -> class registry (spec §12/§13 seam).

NOTE: entry-point auto-discovery is intentionally NOT done here. Plan 4 adds the
allowlisted plugin loader; until then only these built-ins exist."""

from __future__ import annotations

from typing import Any

from mailflow.core.ports import Emitter, Filter
from mailflow.emit.memory import MemoryEmitter
from mailflow.emit.stdout import StdoutEmitter
from mailflow.filters.deterministic import (
    BlacklistFilter,
    InternalDomainFilter,
    ListMailFilter,
    SubjectFilter,
    WhitelistFilter,
)
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

PROVIDER_KINDS = {"memory", "graph", "gmail"}
EMITTER_KINDS = {"memory", "stdout", "pubsub"}
STORE_KINDS = {"memory"}
FILTER_KINDS = {"whitelist", "blacklist", "internal_domain", "subject", "list_mail"}


def build_filter(kind: str, params: dict[str, Any]) -> Filter:
    if kind == "whitelist":
        return WhitelistFilter(domains=set(params.get("domains", [])))
    if kind == "blacklist":
        return BlacklistFilter(domains=set(params.get("domains", [])))
    if kind == "internal_domain":
        return InternalDomainFilter(domains=set(params.get("domains", [])))
    if kind == "subject":
        return SubjectFilter(patterns=list(params.get("patterns", [])))
    if kind == "list_mail":
        return ListMailFilter()
    raise ValueError(f"unknown filter kind {kind!r}")  # validate() guards this earlier


def build_emitter(kind: str) -> Emitter:
    if kind == "memory":
        return MemoryEmitter()
    if kind == "stdout":
        return StdoutEmitter()
    if kind == "pubsub":
        # needs project_id + topic + live SDK -> wired via
        # mailflow.emit.pubsub.build_pubsub_emitter, not the import-light builder.
        raise NotImplementedError(
            "pubsub emitter is wired via mailflow.emit.pubsub.build_pubsub_emitter "
            "(needs project_id + topic)"
        )
    raise ValueError(f"unknown emitter kind {kind!r}")


def build_cursor_store(kind: str) -> InMemoryCursorStore:
    return InMemoryCursorStore()


def build_dedupe_store(kind: str) -> InMemoryDedupeStore:
    return InMemoryDedupeStore()


def build_blob_store(kind: str) -> InMemoryBlobStore:
    return InMemoryBlobStore()
