"""Show the RAW RFC822 message Gmail returns (before mailflow cleans it).

Fetches the latest message (or a given id) via messages.get(format=raw), base64url-
decodes it, and prints the real MIME structure + the raw text with long base64
attachment blobs truncated so it's readable.

  python scripts/show_raw_message.py [message_id]
"""

from __future__ import annotations

import base64
import email
import os
import sqlite3
import sys

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.config import GmailConfig
from mailflow.adapters.gmail.live import HttpxTransport, OAuthTokenProvider
from mailflow.secrets import EnvSecretProvider

BAR = "=" * 72


def main() -> None:
    mailbox = os.environ["GMAIL_MAILBOXES"].split(",")[0].strip()
    if len(sys.argv) > 1:
        mid = sys.argv[1]
    else:
        row = sqlite3.connect("emails.db").execute(
            "SELECT provider_message_id FROM emails ORDER BY id DESC LIMIT 1"
        ).fetchone()
        mid = row[0]

    sp = EnvSecretProvider()
    cfg = GmailConfig.model_validate({
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret_ref": "env://GMAIL_CLIENT_SECRET",
        "oauth_refresh_token_ref": "env://GMAIL_REFRESH_TOKEN",
        "mailboxes": [mailbox],
    })
    token = OAuthTokenProvider(
        client_id=cfg.client_id, client_secret=sp.get(cfg.client_secret_ref),
        refresh_token=sp.get(cfg.oauth_refresh_token_ref),
        token_uri=cfg.token_uri, scopes=cfg.scopes,
    )
    client = GmailClient(base_url=cfg.base_url, token_provider=token, transport=HttpxTransport())

    data = client.get_message_raw(mailbox, mid)
    b64 = str(data.get("raw", ""))
    raw = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))

    print(f"{BAR}\nmessages.get(format=raw)  id={mid}")
    print(f"  sizeEstimate = {data.get('sizeEstimate')} | base64 length = {len(b64)} "
          f"| decoded RFC822 = {len(raw)} bytes\n{BAR}")

    # 1) the MIME tree (what parts exist)
    msg = email.message_from_bytes(raw)
    print("MIME STRUCTURE (the parts inside):")
    for part in msg.walk():
        ctype = part.get_content_type()
        fname = part.get_filename()
        disp = part.get("Content-Disposition", "")
        enc = part.get("Content-Transfer-Encoding", "")
        tag = f" filename={fname!r}" if fname else ""
        print(f"  - {ctype:28} enc={enc:16}{tag}  {disp.split(';')[0]}")
    print(BAR)

    # 2) the literal raw text, with long base64 runs collapsed
    print("RAW RFC822 (headers + body; attachment base64 truncated):\n")
    text = raw.decode("utf-8", errors="replace")
    out, run = [], 0
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) > 70 and " " not in stripped and "@" not in stripped:
            run += 1
            if run <= 2:
                out.append(line[:64] + " …[+base64]")
            elif run == 3:
                out.append("        …[attachment base64 omitted]…")
            continue
        run = 0
        out.append(line)
    print("\n".join(out)[:4000])


if __name__ == "__main__":
    main()
