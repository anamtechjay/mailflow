"""CLI + auth-helper tests. The browser OAuth flow is not unit-tested (it needs a
real Google consent); everything around it — env read/write, arg parsing, credential
resolution, and command dispatch — is."""

from __future__ import annotations

import argparse

import pytest

from mailflow.auth import load_env_file, upsert_env_var
from mailflow.cli import _resolve, build_parser, main

# ---------------------------------------------------------------------------
# .env read / write
# ---------------------------------------------------------------------------

def test_upsert_creates_file_when_absent(tmp_path) -> None:
    p = tmp_path / ".env"
    existed = upsert_env_var("GMAIL_REFRESH_TOKEN", "tok123", path=str(p))
    assert existed is False
    assert "GMAIL_REFRESH_TOKEN=tok123" in p.read_text()


def test_upsert_updates_existing_key_in_place(tmp_path) -> None:
    p = tmp_path / ".env"
    p.write_text("A=1\nGMAIL_REFRESH_TOKEN=old\nB=2\n")
    existed = upsert_env_var("GMAIL_REFRESH_TOKEN", "new", path=str(p))
    assert existed is True
    text = p.read_text()
    assert "GMAIL_REFRESH_TOKEN=new" in text
    assert "GMAIL_REFRESH_TOKEN=old" not in text
    assert "A=1" in text and "B=2" in text          # other lines preserved


def test_upsert_appends_when_key_missing(tmp_path) -> None:
    p = tmp_path / ".env"
    p.write_text("A=1\n")
    upsert_env_var("GMAIL_REFRESH_TOKEN", "tok", path=str(p))
    text = p.read_text()
    assert "A=1" in text and "GMAIL_REFRESH_TOKEN=tok" in text


def test_load_env_file_parses_and_skips_comments(tmp_path) -> None:
    p = tmp_path / ".env"
    p.write_text("# comment\nA=1\n\nB = two \nbroken-no-equals\n")
    env = load_env_file(str(p))
    assert env == {"A": "1", "B": "two"}            # comment/blank/no-= skipped, trimmed


def test_load_env_file_missing_returns_empty(tmp_path) -> None:
    assert load_env_file(str(tmp_path / "nope.env")) == {}


# ---------------------------------------------------------------------------
# credential resolution: flag > env > .env > prompt
# ---------------------------------------------------------------------------

def test_resolve_prefers_flag(monkeypatch) -> None:
    monkeypatch.setenv("GMAIL_CLIENT_ID", "from-env")
    assert _resolve("from-flag", "GMAIL_CLIENT_ID", {"GMAIL_CLIENT_ID": "from-file"},
                    "prompt: ") == "from-flag"


def test_resolve_falls_back_to_env(monkeypatch) -> None:
    monkeypatch.setenv("GMAIL_CLIENT_ID", "from-env")
    assert _resolve(None, "GMAIL_CLIENT_ID", {"GMAIL_CLIENT_ID": "from-file"},
                    "prompt: ") == "from-env"


def test_resolve_falls_back_to_env_file(monkeypatch) -> None:
    monkeypatch.delenv("GMAIL_CLIENT_ID", raising=False)
    assert _resolve(None, "GMAIL_CLIENT_ID", {"GMAIL_CLIENT_ID": "from-file"},
                    "prompt: ") == "from-file"


def test_resolve_prompts_last(monkeypatch) -> None:
    monkeypatch.delenv("GMAIL_CLIENT_ID", raising=False)
    monkeypatch.setattr("builtins.input", lambda _p: "typed")
    assert _resolve(None, "GMAIL_CLIENT_ID", {}, "prompt: ") == "typed"


# ---------------------------------------------------------------------------
# parser + dispatch
# ---------------------------------------------------------------------------

def test_parser_auth_gmail_defaults() -> None:
    args = build_parser().parse_args(["auth", "gmail"])
    assert args.command == "auth" and args.provider == "gmail"
    assert args.env_file == ".env" and args.port == 8080


def test_parser_auth_gmail_flags() -> None:
    args = build_parser().parse_args(
        ["auth", "gmail", "--client-id", "X", "--client-secret", "Y",
         "--env-file", "my.env", "--port", "9090"])
    assert args.client_id == "X" and args.client_secret == "Y"
    assert args.env_file == "my.env" and args.port == 9090


def test_parser_check_gmail() -> None:
    args = build_parser().parse_args(["check", "gmail"])
    assert args.command == "check" and args.provider == "gmail"


def test_main_no_command_prints_help_and_returns_1(capsys) -> None:
    rc = main([])
    assert rc == 1
    assert "usage: mailflow" in capsys.readouterr().out


def test_main_auth_gmail_missing_creds_returns_2(monkeypatch, tmp_path, capsys) -> None:
    # no flags, no env, empty .env, and prompt returns blank -> missing creds -> exit 2
    monkeypatch.delenv("GMAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("GMAIL_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("builtins.input", lambda _p: "")
    rc = main(["auth", "gmail", "--env-file", str(tmp_path / ".env")])
    assert rc == 2
    assert "client id and secret are required" in capsys.readouterr().err
