"""Demo the black-box library features end-to-end (no Gmail needed).

  python scripts/demo_blackbox.py

Shows: the unified filters=[...] (built-in specs + a custom function), composable
stages (modify + drop), the clean_fn hook, and safe-by-default.
"""

from __future__ import annotations

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def raw(mid: str, sender: str, subject: str) -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: {sender}\r\n"
            f"To: ops@acme.com\r\nSubject: {subject}\r\n\r\nbody").encode()


def seed() -> dict:
    return {STREAM: [
        SeedEmail("m1", raw("m1", "alice@partner.com", "Invoice #42")),
        SeedEmail("m2", raw("m2", "bob@gmail.com", "hi there")),      # personal
        SeedEmail("m3", raw("m3", "carol@spam.com", "BUY NOW")),       # blacklisted
        SeedEmail("m4", raw("m4", "dave@client.com", "project update")),
    ]}


def show(title: str, emails: list) -> None:
    print(f"\n--- {title} ---")
    for e in emails:
        print(f"   kept: {e.from_.address:22} | {e.subject}")
    if not emails:
        print("   (nothing kept)")


def main() -> None:
    # 1. Safe-by-default: no filters → everything passes
    show("1) no filters (safe-by-default: all 4 pass)",
         connect("memory", seed=seed(), tenant="acme").fetch_new())

    # 2. Built-in filter specs: drop blacklist + personal domains
    show("2) filters=[blacklist, no_personal]  (drops spam.com + gmail.com)",
         connect("memory", seed=seed(), tenant="acme", filters=[
             {"kind": "blacklist", "domains": ["spam.com"]},
             {"kind": "no_personal"},
         ]).fetch_new())

    # 3. Custom function filter: drop anything with "invoice" in the subject
    show("3) custom function filter  (drops subjects containing 'invoice')",
         connect("memory", seed=seed(), tenant="acme", filters=[
             lambda env: "invoice" not in env.subject.lower(),
         ]).fetch_new())

    # 4. Stages: tag every kept email, then drop personal as a stage
    def tag(email):
        return email.model_copy(update={"subject": "[seen] " + email.subject})

    def drop_personal(email):
        return None if email.from_.address.endswith("@gmail.com") else email

    show("4) stages=[tag, drop_personal]  (modifies subject + drops gmail)",
         connect("memory", seed=seed(), tenant="acme",
                 stages=[tag, drop_personal]).fetch_new())

    # 5. clean_fn: a custom cleaning hook (runs first)
    show("5) clean_fn  (rewrites every subject)",
         connect("memory", seed=seed(), tenant="acme",
                 clean_fn=lambda e: e.model_copy(update={"subject": "CLEANED"})).fetch_new())

    # 6. fields=[...]: declare the data you want -> get only that (a dict)
    print("\n--- 6) fields=['subject','from']  (declare the data -> get only that) ---")
    for d in connect("memory", seed=seed(), tenant="acme",
                     fields=["subject", "from"]).fetch_new():
        print(f"   {d}   (type: {type(d).__name__})")

    print("\nOK - the black-box library features work.\n")


if __name__ == "__main__":
    main()
