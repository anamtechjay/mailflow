"""Microsoft Graph connectivity check using the 4 core app credentials.

Loads .env, accepts either your key names (Tenant_ID / App_Client_ID / SECRET_VALUE /
GRAPH_MAILBOXE) or the standard ones (GRAPH_TENANT_ID / GRAPH_CLIENT_ID /
GRAPH_CLIENT_SECRET / GRAPH_MAILBOXES), then proves the two prerequisites:
  1. app-only token (MSAL client-credentials)
  2. mailbox read (GET /users/{mailbox}/messages)   <- the real test

No Event Hubs / Service Bus needed for THIS check — those are only for live push.

Run: python scripts/check_graph_env.py
"""

from __future__ import annotations

import os
import sys

GRAPH = "https://graph.microsoft.com/v1.0"


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def pick(*names: str) -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return ""


def main() -> int:
    load_env()
    import httpx
    import msal

    tenant = pick("Tenant_ID", "GRAPH_TENANT_ID", "TENANT_ID")
    client_id = pick("App_Client_ID", "GRAPH_CLIENT_ID", "APP_CLIENT_ID")
    secret = pick("SECRET_VALUE", "GRAPH_CLIENT_SECRET")
    mbx_raw = pick("GRAPH_MAILBOXE", "GRAPH_MAILBOXES", "GRAPH_MAILBOX")
    mailboxes = [m.strip() for m in mbx_raw.split(",") if m.strip()]

    missing = [n for n, v in [("Tenant_ID", tenant), ("App_Client_ID", client_id),
                              ("SECRET_VALUE", secret), ("GRAPH_MAILBOXE", mbx_raw)] if not v]
    if missing:
        print(f"[FAIL] missing in .env: {', '.join(missing)}")
        return 1

    print(f"tenant={tenant}\nclient_id={client_id}\nmailboxes={mailboxes}\nsecret=***{secret[-4:]}\n")

    # 1) app-only token
    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        client_credential=secret,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        print(f"[FAIL] token: {result.get('error')}: {result.get('error_description')}")
        print("       -> check Tenant_ID / App_Client_ID / SECRET_VALUE (secret may be expired,")
        print("          or you copied the secret ID instead of its VALUE).")
        return 1
    token = result["access_token"]
    print("[OK]   app-only token acquired")

    # decode the JWT payload to see the actual granted app roles
    import base64
    import json as _json
    try:
        payload_seg = token.split(".")[1]
        payload_seg += "=" * (-len(payload_seg) % 4)
        claims = _json.loads(base64.urlsafe_b64decode(payload_seg))
        roles = claims.get("roles", [])
        print(f"       token roles (granted app permissions): {roles or 'NONE'}")
        print(f"       token audience: {claims.get('aud')}")
        if "Mail.Read" in roles or "Mail.ReadWrite" in roles:
            print("       -> Mail.Read IS in the token. If read still 403s, it's an Exchange")
            print("          Application Access Policy scoping the app away from this mailbox.")
        else:
            print("       -> Mail.Read is NOT in the token. The app permission is missing OR")
            print("          admin consent was not actually granted/propagated yet.")
    except Exception as exc:  # noqa: BLE001
        print(f"       (could not decode token roles: {exc})")
    print()

    # 2) mailbox read
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
                    print(f"[OK]   {mbx}: mailbox readable but empty")
            else:
                ok = False
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                err = body.get("error") or {}
                print(f"[FAIL] {mbx}: HTTP {r.status_code} — {err.get('code')}: {err.get('message')}")

    print("\n" + "=" * 60)
    if ok:
        print("RESULT: token OK + mailbox READ OK. The 4 values are enough to CONNECT & fetch.")
        print("For LIVE auto-receiving you additionally need a transport (Service Bus or Event Hubs).")
    else:
        print("RESULT: token may be fine but mailbox READ failed. If the code is")
        print("'MailboxNotEnabledForRESTAPI' / 'ResourceNotFound' -> that address has NO")
        print("Exchange Online mailbox (it's on Google Workspace) -> Graph won't work for it.")
        print("If '403 / Authorization_RequestDenied' -> add Graph application permission")
        print("'Mail.Read' to the app registration and Grant admin consent.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
