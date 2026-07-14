"""Edge-case coverage for mailflow.logging_setup.enable_logging(): int levels, an
in-memory stream target, text vs JSON formatting, level filtering, re-configuration,
and non-destructive handler coexistence (NullHandler + user handlers).

Each test isolates the shared `mailflow` logger's handler list/level so tests in
this file (and other test modules that also touch the logger) don't bleed into
each other."""

from __future__ import annotations

import io
import json as _json
import logging
from pathlib import Path

import pytest

from mailflow.logging_setup import enable_logging, install_null_handler


@pytest.fixture(autouse=True)
def _isolate_mailflow_logger():
    logger = logging.getLogger("mailflow")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    logger.handlers = []
    yield
    logger.handlers = original_handlers
    logger.level = original_level


def test_enable_logging_accepts_int_level() -> None:
    logger = enable_logging(level=logging.DEBUG, stream=io.StringIO())
    assert logger.level == logging.DEBUG


def test_enable_logging_stream_receives_formatted_line() -> None:
    buf = io.StringIO()
    enable_logging(level="INFO", stream=buf)
    logging.getLogger("mailflow.pipeline").info("hello-stream")
    assert "hello-stream" in buf.getvalue()


def test_text_formatter_contains_level_logger_and_message() -> None:
    buf = io.StringIO()
    enable_logging(level="WARNING", stream=buf, json=False)
    logging.getLogger("mailflow.pipeline").warning("plain-text-line")
    text = buf.getvalue()
    assert "WARNING" in text
    assert "mailflow.pipeline" in text
    assert "plain-text-line" in text


def test_json_formatter_includes_exc_key_on_exception() -> None:
    buf = io.StringIO()
    enable_logging(level="ERROR", stream=buf, json=True)
    logger = logging.getLogger("mailflow.pipeline")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failure occurred")
    rec = _json.loads(buf.getvalue().splitlines()[0])
    assert "exc" in rec
    assert "ValueError" in rec["exc"]


def test_level_filtering_excludes_debug_when_info_configured() -> None:
    buf = io.StringIO()
    enable_logging(level="INFO", stream=buf)
    logging.getLogger("mailflow.pipeline").debug("should-not-appear")
    assert "should-not-appear" not in buf.getvalue()


def test_enable_logging_returns_the_mailflow_logger() -> None:
    logger = enable_logging(stream=io.StringIO())
    assert logger is logging.getLogger("mailflow")


def test_recalling_enable_logging_switches_to_new_file(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_b = tmp_path / "b.log"
    enable_logging(level="INFO", file=str(file_a))
    logging.getLogger("mailflow.pipeline").info("to-a")

    enable_logging(level="INFO", file=str(file_b))
    logging.getLogger("mailflow.pipeline").info("to-b")

    for h in logging.getLogger("mailflow").handlers:
        h.flush()

    assert "to-b" in file_b.read_text()
    assert "to-b" not in file_a.read_text()
    managed = [h for h in logging.getLogger("mailflow").handlers
               if getattr(h, "_mailflow_managed", False)]
    assert len(managed) == 1


def test_enable_logging_preserves_null_handler_and_user_handler() -> None:
    install_null_handler()
    user_handler = logging.Handler()
    logging.getLogger("mailflow").addHandler(user_handler)

    enable_logging(level="INFO", stream=io.StringIO())

    handlers = logging.getLogger("mailflow").handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)
    assert user_handler in handlers
