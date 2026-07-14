"""CFG-3 — the documented `state="sqlite:///mf.db"` must resolve to a cwd-relative
`mf.db`, not to `/mf.db` (an unwritable filesystem-root path that crashes at connect()).

`sqlite:///mf.db` (three slashes + a bare filename) is the well-known three-slash-relative
footgun. A path with an intermediate directory (`sqlite:///tmp/mf.db`) still means the
absolute `/tmp/mf.db` — only the root-level single-filename case is remapped.
"""

from __future__ import annotations

from mailflow.config.state import resolve_state


def test_bare_filename_resolves_relative_not_root():
    stores = resolve_state("sqlite:///mf.db")
    assert stores.cursor.params["path"] == "mf.db"     # relative to cwd, NOT "/mf.db"
    assert stores.dedupe.params["path"] == "mf.db"


def test_absolute_path_with_dir_is_preserved():
    # Regression guard for the existing contract (tests/test_registry.py): a real
    # absolute path keeps its leading slash.
    stores = resolve_state("sqlite:///tmp/mf.db")
    assert stores.cursor.params["path"] == "/tmp/mf.db"


def test_attachments_dir_is_relative_for_bare_filename():
    stores = resolve_state("sqlite:///mf.db")
    # blob dir sits next to the db; for a cwd-relative db that's a cwd-relative folder.
    assert stores.blob.params["directory"] == "attachments"
