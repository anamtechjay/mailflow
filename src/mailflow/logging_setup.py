"""Consumer-facing logging setup. The library ships SILENT (a NullHandler on the
root `mailflow` logger); the app opts in via enable_logging(). We never call
basicConfig and never attach a real handler except here — the app owns its logging."""

from __future__ import annotations

import json as _json
import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import TextIO

_ROOT = "mailflow"


def install_null_handler() -> None:
    """Attach a NullHandler once so `mailflow.*` loggers never emit
    'No handlers could be found' and the app stays in full control."""
    root = logging.getLogger(_ROOT)
    if not any(isinstance(h, logging.NullHandler) for h in root.handlers):
        root.addHandler(logging.NullHandler())


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return _json.dumps(payload)


def enable_logging(
    *,
    level: str | int = "INFO",
    file: str | None = None,
    stream: TextIO | None = None,
    json: bool = False,
    max_bytes: int = 10_485_760,
    backup_count: int = 3,
) -> logging.Logger:
    """Attach a single managed handler to the `mailflow` logger and return it.

    file  -> RotatingFileHandler(file); else stream (default sys.stderr) -> StreamHandler.
    json  -> compact JSON lines; else a readable text line.
    Idempotent: removes any handler this function added previously before adding one."""
    logger = logging.getLogger(_ROOT)
    logger.setLevel(level)
    for h in [h for h in logger.handlers if getattr(h, "_mailflow_managed", False)]:
        logger.removeHandler(h)

    handler: logging.Handler
    if file is not None:
        handler = RotatingFileHandler(
            file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler(stream or sys.stderr)

    handler.setLevel(level)
    handler.setFormatter(
        _JsonFormatter() if json
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
    )
    setattr(handler, "_mailflow_managed", True)
    logger.addHandler(handler)
    return logger
