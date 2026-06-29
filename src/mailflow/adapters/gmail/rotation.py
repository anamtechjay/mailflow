"""A8: a concrete TokenRotationSink that persists a rotated OAuth refresh token to a
JSON file, so the next run can read the current token. Minimal single-tenant POC
default; a consumer may supply their own TokenRotationSink (e.g. a secrets-manager
writer) via connect(overrides={"rotation_sink": ...})."""

from __future__ import annotations

import json
from pathlib import Path


class FileTokenRotationSink:
    """Persists {ref: token} to a JSON file. Implements the TokenRotationSink port."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def on_refresh(self, ref: str, new_token: str) -> None:
        data = self._read()
        data[ref] = new_token
        self._path.write_text(json.dumps(data))

    def load(self, ref: str) -> str | None:
        return self._read().get(ref)

    def _read(self) -> dict[str, str]:
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return {}
        parsed = json.loads(raw)
        return {str(k): str(v) for k, v in parsed.items()}
