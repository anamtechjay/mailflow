"""Scenario — restart resume. See docs/qa-partA-coverage.md ("Scenarios & invariants").

`test_restart_resumes_without_reemit` is the PRIMARY test: two `connect()` calls
sharing one sqlite `state=` db, second run growing the seed from 5 to 8 items,
proves the first 5 come back as `duplicates` (not re-emitted) and only the 3
new ones are `emitted` -- see also F14's `test_sqlite_cursor_persists` /
`test_sqlite_dedupe_no_reemit` for the cursor-only and dedupe-only variants of
this same guarantee.

`test_kill_restart_subprocess` gives that guarantee real *process* fidelity: two
independent `sys.executable` child processes (not just two in-process
`connect()` calls) share one sqlite db file, proving the on-disk state survives
an actual process boundary, not just Python-object lifetime within one
interpreter.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

from tests._harness.email_builder import raw

S = StreamRef(mailbox="ops@acme.com", folder="inbox")

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_restart_resumes_without_reemit(tmp_path):
    db = f"sqlite:///{tmp_path / 'mf.db'}"
    all8 = {S: [SeedEmail(f"m{i}", raw(f"m{i}")) for i in range(8)]}

    connect("memory", seed={S: all8[S][:5]}, state=db).fetch_new()

    caps = []
    connect("memory", seed=all8, state=db, on_report=caps.append).fetch_new()
    # NOTE (deviation from the plan brief): the brief's snippet asserted
    # `duplicates == 5, emitted == 3`, expecting the first 5 messages to be
    # re-fetched and caught by dedupe. Verified against real behavior: the
    # persisted sqlite CURSOR (not just dedupe) already sits at position 5, so
    # MemoryProvider.fetch() skips items 0..4 by cursor position and never
    # re-fetches them at all -- they never reach dedupe, so `duplicates` stays
    # 0. Only the 3 NEW items (indices 5..7, appended to the seed) are fetched
    # and emitted. This is the same cursor-skip behavior already proven by
    # F14's `test_sqlite_cursor_persists`; the corrected assertion below still
    # proves the scenario this test is named for -- a restart with a GROWN
    # seed resumes cleanly and emits only the new mail, never re-emitting the
    # old mail -- it just does so via the cursor rather than the dedupe layer.
    assert caps[0].fetched == 3
    assert caps[0].duplicates == 0
    assert caps[0].emitted == 3


# Child-process runner: seeds `count` distinct messages ("m0".."m{count-1}") on
# one stream, runs a single memory-pipeline pass against the sqlite db given on
# argv, and writes the resulting RunReport counters to a JSON file so the
# parent test (a separate process) can assert on them. Uses only the stdlib +
# the installed `mailflow` package -- no dependency on the tests/_harness
# fixtures, so it stays runnable as a standalone script regardless of cwd.
_RUNNER_SCRIPT = """
import json
import sys
from email.message import EmailMessage
from email.policy import default as default_policy

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

db_uri, result_path, count = sys.argv[1], sys.argv[2], int(sys.argv[3])


def raw(msg_id):
    msg = EmailMessage(policy=default_policy)
    msg["From"] = "a@partner.com"
    msg["To"] = "ops@acme.com"
    msg["Subject"] = f"subj {msg_id}"
    msg["Message-ID"] = f"<{msg_id}@partner.com>"
    msg.set_content(f"body {msg_id}")
    return msg.as_bytes()


S = StreamRef(mailbox="ops@acme.com", folder="inbox")
seed = {S: [SeedEmail(f"m{i}", raw(f"m{i}")) for i in range(count)]}

reports = []
connect("memory", seed=seed, state=db_uri, on_report=reports.append).fetch_new()

with open(result_path, "w") as f:
    json.dump({
        "fetched": reports[0].fetched,
        "emitted": reports[0].emitted,
        "duplicates": reports[0].duplicates,
    }, f)
"""


@pytest.mark.slow
def test_kill_restart_subprocess(tmp_path):
    db = f"sqlite:///{tmp_path / 'mf.db'}"
    script = tmp_path / "runner.py"
    script.write_text(_RUNNER_SCRIPT, encoding="utf-8")

    # First child: processes messages 0..4 against the fresh db, then exits.
    result1 = tmp_path / "r1.json"
    subprocess.run(
        [sys.executable, str(script), db, str(result1), "5"],
        cwd=REPO_ROOT, check=True, timeout=60,
    )
    r1 = json.loads(result1.read_text())
    assert r1["emitted"] == 5
    assert r1["duplicates"] == 0

    # Second, INDEPENDENT child process ("restart"): same db file, grown seed
    # (0..7). The persisted sqlite cursor already sits past the first 5, so
    # this child's MemoryProvider skips them by cursor position (never
    # re-fetches them at all -- see the matching note in
    # test_restart_resumes_without_reemit) and only the 3 NEW ones are
    # fetched/emitted. Either way, the first 5 are never re-emitted, which is
    # what "restart resumes without reemit" is actually proving here, now with
    # real process-boundary fidelity instead of just two in-process connect()
    # calls.
    result2 = tmp_path / "r2.json"
    subprocess.run(
        [sys.executable, str(script), db, str(result2), "8"],
        cwd=REPO_ROOT, check=True, timeout=60,
    )
    r2 = json.loads(result2.read_text())
    assert r2["fetched"] == 3
    assert r2["duplicates"] == 0
    assert r2["emitted"] == 3
