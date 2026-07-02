"""One-time: get a Gmail OAuth refresh token for a mailbox. Opens a browser, you sign
in as the mailbox owner (e.g. jeevananthan.p@techjays.com) and click Allow, then it
prints the refresh token to paste into .env as GMAIL_REFRESH_TOKEN.

Setup:
  pip install google-auth-oauthlib
  # provide the OAuth client you created (Web application, redirect http://localhost:8080):
  set GMAIL_CLIENT_ID=...        (PowerShell:  $env:GMAIL_CLIENT_ID="..." )
  set GMAIL_CLIENT_SECRET=...
  python scripts/get_gmail_refresh_token.py
"""

from __future__ import annotations

import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
REDIRECT_PORT = 8080  # must match the Authorized redirect URI on the OAuth client


def main() -> None:
    client_id = os.environ.get("GMAIL_CLIENT_ID") or input("Client ID: ").strip()
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET") or input("Client secret: ").strip()

    client_config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"http://localhost:{REDIRECT_PORT}/",
                              f"http://localhost:{REDIRECT_PORT}"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    # access_type=offline + prompt=consent guarantee a refresh token is returned.
    creds = flow.run_local_server(port=REDIRECT_PORT, access_type="offline", prompt="consent")

    token = creds.refresh_token or ""
    print("\n" + "=" * 60)
    if token and _write_env(token):
        print("GMAIL_REFRESH_TOKEN written to .env (len=%d)" % len(token))
    else:
        print("GMAIL_REFRESH_TOKEN=" + (token or "<none returned>"))
        print("(.env not found — paste the value above manually)")
    print("=" * 60)


def _write_env(token: str, path: str = ".env") -> bool:
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    out, found = [], False
    for ln in lines:
        if ln.startswith("GMAIL_REFRESH_TOKEN="):
            out.append("GMAIL_REFRESH_TOKEN=" + token)
            found = True
        else:
            out.append(ln)
    if not found:
        out.append("GMAIL_REFRESH_TOKEN=" + token)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return True


if __name__ == "__main__":
    main()
