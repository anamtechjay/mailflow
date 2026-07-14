"""Gmail OAuth helper + a tiny dependency-free `.env` reader/writer.

The browser consent flow (`get_gmail_refresh_token`) needs the `gmail` extra's
`google-auth-oauthlib` and is imported lazily. The pure file helpers
(`load_env_file`, `upsert_env_var`) have no third-party deps and are unit-tested.
"""

from __future__ import annotations

import os

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_REDIRECT_PORT = 8080


def load_env_file(path: str = ".env") -> dict[str, str]:
    """Parse a `.env` file into a dict. `KEY=VALUE` lines only; blanks and `#`
    comments are skipped. Just enough to read back what `upsert_env_var` wrote —
    no python-dotenv dependency."""
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def upsert_env_var(key: str, value: str, *, path: str = ".env") -> bool:
    """Set `KEY=value` in the `.env` file: replace the existing line in place or
    append it, preserving every other line. Returns True if the file already
    existed (updated), False if it was created.

    Because `.env` holds a secret (the refresh token), the file is clamped to
    owner-only `0600` permissions after writing — the same policy `rotation.py`
    applies to rotated tokens. On Windows `chmod` maps only loosely to ACLs, so
    this is best-effort there; `.env` is also git-ignored project-wide."""
    existed = os.path.exists(path)
    lines: list[str] = []
    if existed:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()

    out: list[str] = []
    found = False
    for line in lines:
        if line.startswith(key + "="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")

    # Create (if absent) with 0600; O_CREAT mode is ignored for an existing file,
    # so also chmod afterwards to clamp a previously-looser file to owner-only.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX perms (e.g. some Windows FS)
    return existed


def get_gmail_refresh_token(
    client_id: str,
    client_secret: str,
    *,
    port: int = DEFAULT_REDIRECT_PORT,
    scopes: list[str] | None = None,
) -> str:
    """Run the local-server OAuth consent flow and return the refresh token.

    Opens a browser; the user signs in as the mailbox owner and clicks Allow.
    `access_type=offline` + `prompt=consent` guarantee a refresh token comes back.
    Requires `google-auth-oauthlib` (the `gmail` extra)."""
    from google_auth_oauthlib.flow import InstalledAppFlow  # lazy: gmail extra

    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [
                f"http://localhost:{port}/",
                f"http://localhost:{port}",
            ],
        }
    }
    flow = InstalledAppFlow.from_client_config(
        client_config, scopes=scopes or [GMAIL_READONLY]
    )
    creds = flow.run_local_server(port=port, access_type="offline", prompt="consent")
    return str(creds.refresh_token or "")
