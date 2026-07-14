import json as _json
import logging
from pathlib import Path

from mailflow.logging_setup import enable_logging, install_null_handler


def test_null_handler_is_installed_on_root_mailflow_logger():
    install_null_handler()
    handlers = logging.getLogger("mailflow").handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)


def test_enable_logging_writes_to_file(tmp_path: Path):
    log_file = tmp_path / "mailflow.log"
    enable_logging(level="DEBUG", file=str(log_file))
    logging.getLogger("mailflow.pipeline").info("hello-from-test")
    for h in logging.getLogger("mailflow").handlers:
        h.flush()
    assert "hello-from-test" in log_file.read_text()


def test_enable_logging_json_format_is_parseable(tmp_path: Path):
    log_file = tmp_path / "mf.jsonl"
    enable_logging(level="INFO", file=str(log_file), json=True)
    logging.getLogger("mailflow.pipeline").warning("structured-line")
    for h in logging.getLogger("mailflow").handlers:
        h.flush()
    first = log_file.read_text().splitlines()[0]
    rec = _json.loads(first)
    assert rec["message"] == "structured-line"
    assert rec["level"] == "WARNING"


def test_enable_logging_is_idempotent_does_not_stack_handlers(tmp_path: Path):
    enable_logging(level="INFO", file=str(tmp_path / "a.log"))
    enable_logging(level="INFO", file=str(tmp_path / "b.log"))
    managed = [h for h in logging.getLogger("mailflow").handlers
               if getattr(h, "_mailflow_managed", False)]
    assert len(managed) == 1
