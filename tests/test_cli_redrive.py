"""mailflow redrive — CLI wrapper around core/redrive.py (already tested), surfaced
through the same argparse pattern as auth/check."""

from __future__ import annotations

from mailflow.cli import build_parser, main


def test_redrive_subcommand_parses() -> None:
    parser = build_parser()
    args = parser.parse_args(["redrive", "--state", "sqlite:///x.db", "--tenant", "acme"])
    assert args.command == "redrive"
    assert args.state == "sqlite:///x.db"
    assert args.tenant == "acme"
    assert args.limit is None


def test_redrive_runs_against_empty_store(tmp_path, capsys) -> None:
    db = str(tmp_path / "mf.db")
    code = main(["redrive", "--state", f"sqlite:///{db}", "--tenant", "acme"])
    assert code == 0
    out = capsys.readouterr().out
    assert "examined=0" in out


def test_redrive_rejects_bad_state_cleanly(capsys) -> None:
    code = main(["redrive", "--state", "bogus://x", "--tenant", "acme"])
    assert code == 2
    err = capsys.readouterr().err
    assert "error:" in err


def test_purge_rejects_bad_state_cleanly(capsys) -> None:
    code = main(["purge", "--state", "bogus://x"])
    assert code == 2
    err = capsys.readouterr().err
    assert "error:" in err


def test_purge_runs_against_empty_store(tmp_path, capsys) -> None:
    from mailflow.cli import main

    db = str(tmp_path / "mf.db")
    code = main(["purge", "--state", f"sqlite:///{db}"])
    assert code == 0
    assert "purged 0 expired" in capsys.readouterr().out


def test_purge_actually_removes_expired_records(tmp_path, capsys) -> None:
    from mailflow.cli import main
    from mailflow.stores.sqlite import SqliteDedupeStore

    db = str(tmp_path / "mf.db")
    store = SqliteDedupeStore(db, clock=lambda: 1_000.0)
    store.try_claim("k1", 300)
    store.mark_done("k1", ttl_seconds=1)  # expires at t=1001, already in the past by "now"

    code = main(["purge", "--state", f"sqlite:///{db}"])
    assert code == 0
    assert "purged 1 expired" in capsys.readouterr().out
