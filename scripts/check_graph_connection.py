"""Microsoft Graph connectivity smoke test — does app-only auth work, and is there a
readable Exchange Online mailbox?

This is the FIRST thing to run after the Entra app is set up. It does NOT need Event
Hubs / a webhook — it just proves the two prerequisites for notifications:
  1. App-only token   (MSAL client-credentials with your client secret)
  2. Mailbox read     (GET /users/{mailbox}/messages) — the real test

Reads env: GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_MAILBOXES
(comma-separated UPNs, e.g. vidhya.parkavi@techjays.com).

Run:
  pip install msal httpx          # already installed in this project
  # load .env first (PowerShell):
  #   Get-Content .env | ? {$_ -match '=' -and -not $_.StartsWith('#')} | % { $k,$v = $_ -split '=',2; [Environment]::SetEnvironmentVariable($k.Trim(),$v.Trim()) }
  python scripts/check_graph_connection.py
"""

from __future__ import annotations

import os
import sys

GRAPH = "https://graph.microsoft.com/v1.0"


def main() -> int:
    import httpx
    import msal

    tenant = os.environ["GRAPH_TENANT_ID"]
    client_id = os.environ["GRAPH_CLIENT_ID"]
    secret = os.environ["GRAPH_CLIENT_SECRET"]
    mailboxes = [m.strip() for m in os.environ.get("GRAPH_MAILBOXES", "").split(",") if m.strip()]
    if not mailboxes:
        print("[FAIL] set GRAPH_MAILBOXES to at least one UPN (e.g. you@techjays.com)")
        return 1

    print(f"tenant={tenant}\nclient_id={client_id}\nmailboxes={mailboxes}\n")

    # 1. App-only token
    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        client_credential=secret,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        print(f"[FAIL] token: {result.get('error')}: {result.get('error_description')}")
        return 1
    token = result["access_token"]
    print("[OK]   app-only token acquired\n")

    headers = {"Authorization": f"Bearer {token}"}
    ok = True
    with httpx.Client(timeout=30.0) as client:
        for mbx in mailboxes:
            url = f"{GRAPH}/users/{mbx}/messages?$top=1&$select=subject,from,receivedDateTime"
            r = client.get(url, headers=headers)
            if r.status_code == 200:
                msgs = r.json().get("value", [])
                if msgs:
                    m = msgs[0]
                    frm = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
                    print(f"[OK]   {mbx}: READ works — latest subject={m.get('subject')!r} from={frm}")
                else:
                    print(f"[OK]   {mbx}: mailbox readable but empty (no messages)")
            else:
                ok = False
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                err = (body.get("error") or {})
                print(f"[FAIL] {mbx}: HTTP {r.status_code} — {err.get('code')}: {err.get('message')}")

    print("\n" + ("=" * 60))
    if ok:
        print("RESULT: Graph can READ this mailbox -> notifications ARE possible.")
        print("Next: provision Event Hubs + run run_service to get live pushes.")
    else:
        print("RESULT: Graph could NOT read the mailbox. If the error is")
        print("'MailboxNotEnabledForRESTAPI' / 'ResourceNotFound', this tenant has NO")
        print("Exchange Online mailbox (mail is elsewhere, e.g. Google Workspace) ->")
        print("Graph notifications are NOT possible for it. Use a real Exchange mailbox.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
