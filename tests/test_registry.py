"""Registry store selection + config validation for the sqlite/local/postgres kinds."""

from __future__ import annotations

import os

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

POSTGRES_DSN = os.environ.get("MAILFLOW_TEST_POSTGRES_DSN", "")
_needs_postgres = pytest.mark.skipif(
    not POSTGRES_DSN, reason="MAILFLOW_TEST_POSTGRES_DSN not set"
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


@pytest.mark.live
@_needs_postgres
def test_build_postgres_stores() -> None:
    from mailflow.stores.postgres import PostgresCursorStore, PostgresDedupeStore

    params = {"dsn": POSTGRES_DSN}
    assert isinstance(build_cursor_store("postgres", params), PostgresCursorStore)
    assert isinstance(build_dedupe_store("postgres", params), PostgresDedupeStore)


@pytest.mark.live
@_needs_postgres
def test_build_dead_letter_store_postgres() -> None:
    from mailflow.stores.postgres import PostgresDeadLetterStore

    store = build_dead_letter_store("postgres", {"dsn": POSTGRES_DSN})
    assert isinstance(store, PostgresDeadLetterStore)


def test_resolve_state_postgres() -> None:
    dsn = "postgresql://user:pw@localhost:5432/mailflow"
    stores = resolve_state(dsn)
    assert stores.cursor.kind == "postgres"
    assert stores.cursor.params["dsn"] == dsn
    assert stores.dedupe.kind == "postgres"
    assert stores.dedupe.params["dsn"] == dsn
    assert stores.blob.kind == "local"


@_needs_postgres
def test_validate_accepts_postgres() -> None:
    cfg = MailflowConfig.model_validate(
        {"tenant": "t", "stores": resolve_state(POSTGRES_DSN).model_dump()}
    )
    validate(cfg)  # must not raise


def test_resolve_state_memory_includes_dead_letter() -> None:
    stores = resolve_state("memory")
    assert stores.dead_letter.kind == "memory"


def test_resolve_state_sqlite_includes_dead_letter() -> None:
    stores = resolve_state("sqlite:///tmp/mf.db")
    assert stores.dead_letter.kind == "sqlite"
    assert stores.dead_letter.params["path"] == "/tmp/mf.db"


def test_resolve_state_postgres_includes_dead_letter() -> None:
    dsn = "postgresql://user:pw@localhost:5432/mailflow"
    stores = resolve_state(dsn)
    assert stores.dead_letter.kind == "postgres"
    assert stores.dead_letter.params["dsn"] == dsn
