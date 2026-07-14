"""F14 — State (sqlite), e2e restart. See docs/qa-partA-coverage.md.

Drives the public `connect()` facade (not the internal harness) with
`state="sqlite:///<path>"` across TWO separate `connect()` calls sharing the
same .db file, proving cursor + dedupe survive a process restart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from mailflow.core.models import StreamRef
from mailflow.core.observability import RunReport
from mailflow.facade import connect
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryCursorStore

from tests._harness.email_builder import raw

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


def _capture() -> tuple[list[RunReport], Callable[[RunReport], None]]:
    reports: list[RunReport] = []
    return reports, reports.append


def test_sqlite_cursor_persists(tmp_path: Path) -> None:
    db_uri = "sqlite:///" + str(tmp_path / "mf.db")
    seed = {S: [SeedEmail("m1", raw("m1")), SeedEmail("m2", raw("m2")), SeedEmail("m3", raw("m3"))]}

    reports1, on_report1 = _capture()
    mf1 = connect("memory", seed=seed, state=db_uri, on_report=on_report1)
    mf1.run()
    assert reports1[0].emitted == 3

    # Second connect() over the SAME db file and the SAME seed: the persisted cursor
    # tells MemoryProvider.fetch() to skip everything already delivered, so run 2
    # fetches nothing at all (not even a duplicate) -- the resume is at the provider
    # cursor position, not just the dedupe layer.
    reports2, on_report2 = _capture()
    mf2 = connect("memory", seed=seed, state=db_uri, on_report=on_report2)
    mf2.run()
    assert reports2[0].emitted == 0
    assert reports2[0].duplicates == 0
    assert reports2[0].fetched == 0


def test_sqlite_dedupe_no_reemit(tmp_path: Path) -> None:
    db_uri = "sqlite:///" + str(tmp_path / "mf2.db")
    seed = {S: [SeedEmail("m1", raw("m1")), SeedEmail("m2", raw("m2")), SeedEmail("m3", raw("m3"))]}

    reports1, on_report1 = _capture()
    mf1 = connect("memory", seed=seed, state=db_uri, on_report=on_report1)
    mf1.run()
    assert reports1[0].emitted == 3

    # Second run: override cursor_store with a FRESH in-memory one (so the provider
    # replays the whole seed from the top, isolating the effect under test), but keep
    # dedupe on the SAME sqlite file via `state=`. The persisted claims must still
    # suppress re-emission: this proves dedupe -- not just the cursor -- survives a
    # restart.
    reports2, on_report2 = _capture()
    mf2 = connect(
        "memory", seed=seed, state=db_uri, on_report=on_report2,
        overrides={"cursor_store": InMemoryCursorStore()},
    )
    mf2.run()
    assert reports2[0].emitted == 0
    assert reports2[0].duplicates == 3
