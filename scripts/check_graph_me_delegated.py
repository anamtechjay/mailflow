"""Delegated ("/me") Graph check via device-code sign-in — the '/me' path your lead
suggested. Unlike app-only auth, this signs a REAL USER in, so /me/messages works and
reads that user's own mailbox. Often needs only USER consent (no global admin).

Prerequisites on the app registration (Entra):
  1. Authentication -> "Allow public client flows" = Yes  (enables device code)
  2. API permissions -> Microsoft Graph -> DELEGATED -> Mail.Read
     (admin consent may not be required — the user consents at sign-in)

Run it (interactive — needs a browser sign-in):
  python scripts/check_graph_me_delegated.py
It prints a URL + code; open the URL, enter the code, sign in as the mailbox user.
"""

from __future__ import annotations

import os
import sys

GRAPH = "https://graph.microsoft.com/v1.0"


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def pick(*names: str) -> str:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return ""


def main() -> int:
    load_env()
    import httpx
    import msal

    tenant = pick("Tenant_ID", "GRAPH_TENANT_ID")
    client_id = pick("App_Client_ID", "GRAPH_CLIENT_ID")
    if not tenant or not client_id:
        print("[FAIL] need Tenant_ID and App_Client_ID in .env")
        return 1

    # Public client (no secret) — delegated device-code flow
    app = msal.PublicClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
    )

    flow = app.initiate_device_flow(scopes=["Mail.Read"])
    if "user_code" not in flow:
        print(f"[FAIL] could not start device flow: {flow.get('error')}: {flow.get('error_description')}")
        print("       -> enable 'Allow public client flows' = Yes on the app registration.")
        return 1

    print("\n" + "=" * 60)
    print(flow["message"])   # "To sign in, open https://microsoft.com/devicelogin and enter CODE"
    print("=" * 60 + "\n(waiting for you to sign in...)\n")

    result = app.acquire_token_by_device_flow(flow)   # blocks until sign-in completes
    if "access_token" not in result:
        print(f"[FAIL] token: {result.get('error')}: {result.get('error_description')}")
        return 1
    token = result["access_token"]
    print("[OK]   delegated token acquired (a user is now signed in)\n")

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=30.0) as client:
        # who am I?
        who = client.get(f"{GRAPH}/me?$select=displayName,userPrincipalName", headers=headers)
        if who.status_code == 200:
            j = who.json()
            print(f"[OK]   signed in as: {j.get('displayName')} <{j.get('userPrincipalName')}>")

        # the real mail read via /me
        r = client.get(f"{GRAPH}/me/messages?$top=3&$select=subject,from,receivedDateTime", headers=headers)
        if r.status_code == 200:
            msgs = r.json().get("value", [])
            if not msgs:
                print("[OK]   /me/messages works — mailbox is empty")
            for m in msgs:
                frm = (m.get("from") or {}).get("emailAddress", {}).get("address", "?")
                print(f"[MAIL] {m.get('receivedDateTime')}  from={frm}  subject={m.get('subject')!r}")
            print("\nRESULT: '/me' (delegated) READ WORKS. This path needs no admin-consented")
            print("app permission — just the signed-in user's delegated Mail.Read.")
            return 0
        else:
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            err = body.get("error") or {}
            print(f"[FAIL] /me/messages: HTTP {r.status_code} — {err.get('code')}: {err.get('message')}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
