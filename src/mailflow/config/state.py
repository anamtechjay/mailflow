"""Resolve a `state=` string into a StoresConfig.

One small, future-proof knob for the library facade:

    "memory"                -> in-memory cursor/dedupe/blob (stateless; the consuming
                               app dedupes on CleanEmail.canonical_id)
    "sqlite:///path/mf.db"  -> persistent cursor/dedupe in that .db file, attachments
                               on local disk next to it (restart-safe)

Other schemes (e.g. "postgres://…") are reserved and raise NotImplementedError, so the
URL shape can grow without changing callers.
"""

from __future__ import annotations

import os
import re

from mailflow.config.schema import ComponentConfig, StoresConfig

_SQLITE_PREFIX = "sqlite://"
# A leading "/" before a Windows drive ("/C:/…") is a URI artifact — strip it.
_WIN_DRIVE = re.compile(r"^/[A-Za-z]:")


def resolve_state(state: str) -> StoresConfig:
    if state == "memory":
        return StoresConfig()  # defaults are all kind="memory"

    if state.startswith(_SQLITE_PREFIX):
        path = state[len(_SQLITE_PREFIX):]  # "sqlite:///tmp/mf.db" -> "/tmp/mf.db"
        if _WIN_DRIVE.match(path):
            path = path[1:]                 # "/C:/Users/…" -> "C:/Users/…"
        if not path:
            raise ValueError(f"sqlite state needs a path, got {state!r}")
        base = os.path.dirname(path)
        attach_dir = os.path.join(base, "attachments") if base else "attachments"
        sqlite_params = {"path": path}
        return StoresConfig(
            cursor=ComponentConfig(kind="sqlite", params=dict(sqlite_params)),
            dedupe=ComponentConfig(kind="sqlite", params=dict(sqlite_params)),
            blob=ComponentConfig(kind="local", params={"directory": attach_dir}),
        )

    scheme = state.split("://", 1)[0] if "://" in state else state
    raise NotImplementedError(f"unsupported state {scheme!r}: {state!r}")
