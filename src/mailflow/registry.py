"""Built-in kind -> class registry (spec §12/§13 seam).

NOTE: entry-point auto-discovery is intentionally NOT done here. Plan 4 adds the
allowlisted plugin loader; until then only these built-ins exist."""

from __future__ import annotations

from typing import Any

from mailflow.core.ports import BlobStore, CursorStore, DedupeStore, Emitter, Filter
from mailflow.emit.memory import MemoryEmitter
from mailflow.emit.stdout import StdoutEmitter
from mailflow.filters.deterministic import (
    BlacklistFilter,
    BlockSenderFilter,
    InternalDomainFilter,
    ListMailFilter,
    NoPersonalFilter,
    OnlyDomainFilter,
    OnlySenderFilter,
    SubjectFilter,
    WhitelistFilter,
)
from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)
from mailflow.stores.sqlite import SqliteCursorStore, SqliteDedupeStore

PROVIDER_KINDS = {"memory", "graph", "gmail"}
EMITTER_KINDS = {"memory", "stdout", "pubsub"}
STORE_KINDS = {"memory", "sqlite", "local"}
FILTER_KINDS = {
    "whitelist", "blacklist", "internal_domain", "subject", "list_mail", "no_personal",
    "only_domain", "only_sender", "block_sender",
}


def build_filter(kind: str, params: dict[str, Any]) -> Filter:
    if kind == "whitelist":
        return WhitelistFilter(domains=set(params.get("domains", [])))
    if kind == "only_domain":
        return OnlyDomainFilter(domains=set(params.get("domains", [])))
    if kind == "only_sender":
        return OnlySenderFilter(addresses=set(params.get("addresses", [])))
    if kind == "block_sender":
        return BlockSenderFilter(addresses=set(params.get("addresses", [])))
    if kind == "blacklist":
        return BlacklistFilter(domains=set(params.get("domains", [])))
    if kind == "internal_domain":
        return InternalDomainFilter(domains=set(params.get("domains", [])))
    if kind == "subject":
        return SubjectFilter(patterns=list(params.get("patterns", [])))
    if kind == "list_mail":
        return ListMailFilter()
    if kind == "no_personal":
        domains = params.get("domains")
        return NoPersonalFilter(domains=set(domains) if domains else None)
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


def build_cursor_store(kind: str, params: dict[str, Any]) -> CursorStore:
    if kind == "memory":
        return InMemoryCursorStore()
    if kind == "sqlite":
        return SqliteCursorStore(str(params["path"]))
    raise ValueError(f"unknown cursor store kind {kind!r}")


def build_dedupe_store(kind: str, params: dict[str, Any]) -> DedupeStore:
    if kind == "memory":
        return InMemoryDedupeStore()
    if kind == "sqlite":
        return SqliteDedupeStore(str(params["path"]))
    raise ValueError(f"unknown dedupe store kind {kind!r}")


def build_blob_store(kind: str, params: dict[str, Any]) -> BlobStore:
    if kind == "memory":
        return InMemoryBlobStore()
    if kind == "local":
        return LocalBlobStore(str(params.get("directory", "attachments")))
    raise ValueError(f"unknown blob store kind {kind!r}")
