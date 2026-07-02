"""Manual check of the extraction edge-case findings (E-1, E-2).

Run it to SEE the two defects with your own eyes — no pytest needed:

  python scripts/check_extract_findings.py

It feeds hand-built malformed emails straight into the MimeExtractor and prints
what comes back, so you can verify the findings manually.
"""

from __future__ import annotations

import base64

from mailflow.extract.mime import MimeExtractor
from mailflow.stores.memory import InMemoryBlobStore

EX = MimeExtractor()


def extract(raw: bytes, **kw):
    return EX.extract_bytes(
        raw, provider="memory", provider_message_id="pm1",
        stream_id="Inbox", watched_mailbox="ops@acme.com", **kw)


def rule(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


# ---------------------------------------------------------------------------
# FINDING E-1 — unknown charset crashes extraction
# ---------------------------------------------------------------------------
rule("E-1  unknown charset -> does it crash?")
bad = (b"From: a@x.com\r\nSubject: hi\r\nMessage-ID: <m@x>\r\n"
       b"Content-Type: text/plain; charset=x-totally-made-up\r\n\r\n"
       b"still readable")
print("input: a text/plain body that declares charset=x-totally-made-up")
try:
    e = extract(bad)
    print(f"RESULT: delivered OK, body_text = {e.body_text!r}   <- robust")
except Exception as ex:
    print(f"RESULT: *** CRASHED *** {type(ex).__name__}: {ex}")
    print("        -> in the pipeline this email POISONS to the DLQ (not delivered).")


# ---------------------------------------------------------------------------
# FINDING E-2 — is_inline inconsistent between delivered vs stripped
# ---------------------------------------------------------------------------
rule("E-2  Content-ID image WITH a name but NO Content-Disposition -> is_inline?")
img = base64.b64encode(b"\x89PNG pixel data")
raw = (b"Message-ID: <att@x>\r\nFrom: a@x.com\r\nTo: ops@acme.com\r\n"
       b"Subject: pic\r\n"
       b'Content-Type: multipart/mixed; boundary="BB"\r\n\r\n'
       b"--BB\r\nContent-Type: text/plain\r\n\r\nhello\r\n"
       b'--BB\r\nContent-Type: image/png; name="logo.png"\r\n'
       b"Content-ID: <logo1>\r\n"
       b"Content-Transfer-Encoding: base64\r\n\r\n" + img + b"\r\n--BB--\r\n")
e = extract(raw, blob_store=InMemoryBlobStore())
att = e.attachments[0]
print(f"part has: Content-ID=<logo1>, name=logo.png, NO Content-Disposition")
print(f"DELIVERED Attachment.is_inline = {att.is_inline}")
print("  (a Content-ID part is intuitively inline, but it's classified as a real")
print("   attachment here because it has a filename and no explicit disposition.")
print("   In POLICY/strip mode the SAME part is is_inline=True -> inconsistent.)")


# ---------------------------------------------------------------------------
# CONTRAST — the things that DO work (so you trust the rest)
# ---------------------------------------------------------------------------
rule("OK  robustness spot-checks (these should all print cleanly)")
checks = {
    "no From header": b"To: ops@acme.com\r\nSubject: hi\r\nMessage-ID: <m@x>\r\n\r\nbody",
    "no Message-ID":  b"From: a@x.com\r\nSubject: hi\r\n\r\nbody",
    "junk Date":      b"From: a@x.com\r\nDate: not-a-date\r\nMessage-ID: <m@x>\r\n\r\nbody",
    "empty body":     b"From: a@x.com\r\nSubject: hi\r\nMessage-ID: <m@x>\r\n\r\n",
}
for name, raw in checks.items():
    try:
        e = extract(raw)
        print(f"  [OK]   {name:<16} -> from={e.from_.address!r} subj={e.subject!r} "
              f"date={e.date_utc} body={e.body_text!r}")
    except Exception as ex:
        print(f"  [FAIL] {name:<16} -> {type(ex).__name__}: {ex}")

print("\nDONE.")
