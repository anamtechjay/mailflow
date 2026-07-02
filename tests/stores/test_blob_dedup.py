"""Explicit content-addressed dedupe at the BlobStore layer (V1 fast-follow):
an identical ref is written once; a second put_stream is a no-op skip."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.memory import InMemoryBlobStore


def _counting_chunks(tag: str, log: list[str]) -> Iterator[bytes]:
    log.append(tag)  # runs only when the generator is actually consumed
    yield b"PDFBYTES"


def test_inmemory_skips_rewrite_for_existing_ref() -> None:
    store = InMemoryBlobStore()
    log: list[str] = []
    store.put_stream("hash-abc", _counting_chunks("first", log), "application/pdf")
    store.put_stream("hash-abc", _counting_chunks("second", log), "application/pdf")
    assert log == ["first"]  # second write skipped — generator never consumed
    assert store._blobs == {"hash-abc": b"PDFBYTES"}


def test_local_skips_rewrite_for_existing_ref(tmp_path: Path) -> None:
    store = LocalBlobStore(directory=str(tmp_path))
    log: list[str] = []
    store.put_stream("hash-abc", _counting_chunks("first", log), "application/pdf")
    mtime = os.path.getmtime(tmp_path / "hash-abc")
    store.put_stream("hash-abc", _counting_chunks("second", log), "application/pdf")
    assert log == ["first"]  # second write skipped
    assert os.path.getmtime(tmp_path / "hash-abc") == mtime
