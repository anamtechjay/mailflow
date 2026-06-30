"""Registry store selection + config validation for the sqlite/local kinds."""

from __future__ import annotations

import pytest

from mailflow.config.schema import MailflowConfig
from mailflow.config.loader import validate
from mailflow.config.state import resolve_state
from mailflow.core.errors import ConfigError
from mailflow.registry import (
    build_blob_store,
    build_cursor_store,
    build_dead_letter_store,
    build_dedupe_store,
)
from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.memory import (
    InMemoryCursorStore,
    InMemoryDeadLetterStore,
    InMemoryDedupeStore,
)
from mailflow.stores.sqlite import (
    SqliteCursorStore,
    SqliteDeadLetterStore,
    SqliteDedupeStore,
)


def test_build_memory_stores() -> None:
    assert isinstance(build_cursor_store("memory", {}), InMemoryCursorStore)
    assert isinstance(build_dedupe_store("memory", {}), InMemoryDedupeStore)


def test_build_sqlite_stores(tmp_path) -> None:
    params = {"path": str(tmp_path / "mf.db")}
    assert isinstance(build_cursor_store("sqlite", params), SqliteCursorStore)
    assert isinstance(build_dedupe_store("sqlite", params), SqliteDedupeStore)


def test_build_local_blob(tmp_path) -> None:
    assert isinstance(build_blob_store("local", {"directory": str(tmp_path)}), LocalBlobStore)


def test_resolve_state_memory() -> None:
    stores = resolve_state("memory")
    assert stores.cursor.kind == "memory" and stores.dedupe.kind == "memory"


def test_resolve_state_sqlite() -> None:
    stores = resolve_state("sqlite:///tmp/mf.db")
    assert stores.cursor.kind == "sqlite"
    assert stores.cursor.params["path"] == "/tmp/mf.db"
    assert stores.blob.kind == "local"


def test_validate_accepts_sqlite(tmp_path) -> None:
    cfg = MailflowConfig.model_validate(
        {"tenant": "t", "stores": resolve_state(f"sqlite:///{tmp_path}/mf.db").model_dump()}
    )
    validate(cfg)  # must not raise


def test_validate_rejects_unknown_store() -> None:
    cfg = MailflowConfig.model_validate({"tenant": "t", "stores": {"cursor": {"kind": "bogus"}}})
    with pytest.raises(ConfigError):
        validate(cfg)


def test_build_dead_letter_store_memory() -> None:
    assert isinstance(build_dead_letter_store("memory", {}), InMemoryDeadLetterStore)


def test_build_dead_letter_store_sqlite(tmp_path) -> None:
    store = build_dead_letter_store("sqlite", {"path": str(tmp_path / "dlq.db")})
    assert isinstance(store, SqliteDeadLetterStore)
