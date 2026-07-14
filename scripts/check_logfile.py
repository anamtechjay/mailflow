"""Way 2 — the log file, end to end.

Runs one email through the real mailflow pipeline with `log_file=` set, then reads
the file back so you can confirm the log was written.

Run:  python scripts/check_logfile.py

(This uses the in-memory provider to stand in for "the mail you send" — receiving a
real external email needs a live Gmail/Graph connection with credentials. The pipeline,
filtering, and logging below are the exact same code the live path uses.)
"""

from __future__ import annotations

import os

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

# --- 1. the mail "you send" (a normal RFC822 message) ---------------------------------
MY_MAILBOX = "ops@acme.com"
INCOMING = (
    "Message-ID: <invoice-42@partner.com>\r\n"
    "From: Alice <alice@partner.com>\r\n"
    f"To: {MY_MAILBOX}\r\n"
    "Subject: Invoice #42 for June\r\n"
    "\r\n"
    "Hi team, please find invoice #42 attached. Thanks, Alice\r\n"
).encode()

seed = {StreamRef(mailbox=MY_MAILBOX, folder="inbox"): [SeedEmail("MSG1", INCOMING)]}

# --- 2. the log file (Way 2) ----------------------------------------------------------
LOG_FILE = os.path.join(os.getcwd(), "mailflow.log")
if os.path.exists(LOG_FILE):
    os.remove(LOG_FILE)  # start clean so we see only this run

# --- 3. run the app: connect with log_file, process the mail --------------------------
mf = connect(
    "memory",
    seed=seed,
    log_file=LOG_FILE,     # <-- Way 2: write logs to this file
    log_level="INFO",
)
emails = mf.fetch_new()

# --- 4. check everything worked -------------------------------------------------------
print("=" * 68)
print("RESULT")
print("=" * 68)
print(f"emails delivered : {len(emails)}")
if emails:
    e = emails[0]
    print(f"  from           : {e.from_.address}")
    print(f"  subject        : {e.subject}")
    print(f"  canonical_id   : {e.canonical_id}")

print()
print(f"log file         : {LOG_FILE}")
print(f"log file exists  : {os.path.exists(LOG_FILE)}")
print("--- log file contents ---")
with open(LOG_FILE, encoding="utf-8") as fh:
    contents = fh.read()
print(contents or "(empty)")

ok = os.path.exists(LOG_FILE) and "disposition=emitted" in contents and len(emails) == 1
print("=" * 68)
print("CHECK:", "PASS - mail processed and written to the log file" if ok else "FAIL")
print("=" * 68)
