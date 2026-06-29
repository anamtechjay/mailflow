"""A8 (concrete persistence): FileTokenRotationSink persists a rotated refresh token
and reads it back on the next run, satisfying the TokenRotationSink contract."""

from __future__ import annotations

from pathlib import Path

from mailflow.adapters.gmail.rotation import FileTokenRotationSink
from mailflow.core.ports import TokenRotationSink


def test_file_rotation_sink_is_a_token_rotation_sink() -> None:
    assert isinstance(FileTokenRotationSink("/tmp/x.json"), TokenRotationSink)


def test_persists_and_reloads_rotated_token(tmp_path: Path) -> None:
    p = str(tmp_path / "tokens.json")
    FileTokenRotationSink(p).on_refresh("refresh_token_ref", "rotated-1")
    # a fresh sink (next run) reads the persisted value back
    assert FileTokenRotationSink(p).load("refresh_token_ref") == "rotated-1"


def test_load_missing_ref_returns_none(tmp_path: Path) -> None:
    assert FileTokenRotationSink(str(tmp_path / "absent.json")).load("nope") is None


def test_overwrites_on_subsequent_rotation(tmp_path: Path) -> None:
    p = str(tmp_path / "t.json")
    sink = FileTokenRotationSink(p)
    sink.on_refresh("r", "v1")
    sink.on_refresh("r", "v2")
    assert FileTokenRotationSink(p).load("r") == "v2"
