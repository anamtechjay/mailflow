"""Shared store/sink fixtures for the QA suite (see tests/_harness for builders).

`mem_stores` / `sink` are the exact Phase-0 fixtures. `sqlite_stores` is additive
(per the File Structure comment) for later phases that need restart-safety
(persistent cursor/dedupe) — there is no SqliteBlobStore adapter in src/, so the
blob slot falls back to the in-memory one.
"""

from __future__ import annotations

import pytest

from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore
from mailflow.stores.sqlite import SqliteCursorStore, SqliteDedupeStore


@pytest.fixture
def mem_stores():
    return dict(cursor_store=InMemoryCursorStore(),
                dedupe_store=InMemoryDedupeStore(), blob_store=InMemoryBlobStore())


@pytest.fixture
def sqlite_stores(tmp_path):
    return dict(
        cursor_store=SqliteCursorStore(str(tmp_path / "cursor.db")),
        dedupe_store=SqliteDedupeStore(str(tmp_path / "dedupe.db")),
        blob_store=InMemoryBlobStore(),
    )


@pytest.fixture
def sink():
    return MemoryEmitter()
