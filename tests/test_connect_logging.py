import logging
from pathlib import Path

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

RAW = (b"From: a@x.com\r\nTo: me@acme.com\r\nSubject: s\r\n"
       b"Message-ID: <m@x.com>\r\n\r\nbody\r\n")


def _seed():
    return {StreamRef(mailbox="me@acme.com", folder="inbox"): [SeedEmail("M1", RAW)]}


def test_connect_log_file_writes_pipeline_logs(tmp_path: Path):
    log_file = tmp_path / "run.log"
    mf = connect("memory", seed=_seed(), log_file=str(log_file), log_level="INFO")
    mf.fetch_new()
    for h in logging.getLogger("mailflow").handlers:
        h.flush()
    text = log_file.read_text()
    assert "disposition=emitted" in text


def test_connect_on_trace_receives_each_decision():
    seen = []
    connect("memory", seed=_seed(), on_trace=seen.append).fetch_new()
    assert [t.disposition.value for t in seen] == ["emitted"]
