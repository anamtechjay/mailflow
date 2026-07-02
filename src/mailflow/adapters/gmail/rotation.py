"""A8: a concrete TokenRotationSink that persists a rotated OAuth refresh token to a
JSON file, so the next run can read the current token. Minimal single-tenant POC
default; a consumer may supply their own TokenRotationSink (e.g. a secrets-manager
writer) via connect(overrides={"rotation_sink": ...})."""

from __future__ import annotations

import json
import os
from pathlib import Path


class FileTokenRotationSink:
    """Persists {ref: token} to a JSON file. Implements the TokenRotationSink port.

    The file holds an OAuth refresh token (a credential), so it is written with
    owner-only `0600` permissions (and re-chmod'd to enforce that on a pre-existing file).
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def on_refresh(self, ref: str, new_token: str) -> None:
        data = self._read()
        data[ref] = new_token
        # Create (if absent) with 0600; O_CREAT mode is ignored for an existing file, so
        # also chmod afterwards to clamp a previously-looser file down to owner-only.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data))
        os.chmod(self._path, 0o600)

    def load(self, ref: str) -> str | None:
        return self._read().get(ref)

    def _read(self) -> dict[str, str]:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return {}
        parsed = json.loads(raw)
        return {str(k): str(v) for k, v in parsed.items()}
