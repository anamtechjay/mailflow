"""End-to-end: filters wired into connect() drop the right seeded emails."""

from __future__ import annotations

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _raw(mid: str, sender: str, subject: str = "hi") -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: {sender}\r\n"
            f"To: ops@acme.com\r\nSubject: {subject}\r\n\r\nbody").encode()


def _seed() -> dict:
    return {STREAM: [
        SeedEmail("m1", _raw("m1", "alice@partner.com", "Invoice 42")),
        SeedEmail("m2", _raw("m2", "bob@gmail.com", "hello")),
        SeedEmail("m3", _raw("m3", "carol@spam.com", "buy now")),
    ]}


def test_no_filters_passes_everything() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme")
    assert len(mf.fetch_new()) == 3            # safe-by-default: nothing dropped


def test_blacklist_spec_drops() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme",
                 filters=[{"kind": "blacklist", "domains": ["spam.com"]}])
    out = mf.fetch_new()
    assert {e.from_.address for e in out} == {"alice@partner.com", "bob@gmail.com"}


def test_only_domain_keeps_just_that_domain() -> None:
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", "alice@partner.com")),
        SeedEmail("m2", _raw("m2", "bob@gmail.com")),
        SeedEmail("m3", _raw("m3", "carol@spam.com")),
    ]}
    mf = connect("memory", seed=seed, tenant="acme",
                 filters=[{"kind": "only_domain", "domains": ["partner.com"]}])
    out = mf.fetch_new()
    assert {e.from_.address for e in out} == {"alice@partner.com"}   # ONLY partner.com


def test_block_sender_drops_just_that_person() -> None:
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", "jeevaskp1308@gmail.com")),   # blocked
        SeedEmail("m2", _raw("m2", "someone-else@gmail.com")),   # other gmail passes
        SeedEmail("m3", _raw("m3", "alice@partner.com")),
    ]}
    mf = connect("memory", seed=seed, tenant="acme",
                 filters=[{"kind": "block_sender", "addresses": ["jeevaskp1308@gmail.com"]}])
    out = mf.fetch_new()
    kept = {e.from_.address for e in out}
    assert "jeevaskp1308@gmail.com" not in kept             # the one address is dropped
    assert kept == {"someone-else@gmail.com", "alice@partner.com"}  # others pass (incl. gmail)


def test_only_sender_keeps_just_that_person() -> None:
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", "alice@partner.com")),
        SeedEmail("m2", _raw("m2", "bob@partner.com")),
    ]}
    mf = connect("memory", seed=seed, tenant="acme",
                 filters=[{"kind": "only_sender", "addresses": ["alice@partner.com"]}])
    out = mf.fetch_new()
    assert {e.from_.address for e in out} == {"alice@partner.com"}   # ONLY alice, not bob


def test_only_domain_empty_is_safe_noop() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme",
                 filters=[{"kind": "only_domain", "domains": []}])
    assert len(mf.fetch_new()) == 3                                   # empty -> no-op


def test_no_personal_drops_gmail() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme",
                 filters=[{"kind": "no_personal"}])
    out = mf.fetch_new()
    assert "bob@gmail.com" not in {e.from_.address for e in out}


def test_custom_function_filter() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme",
                 filters=[lambda env: "invoice" not in env.subject.lower()])
    out = mf.fetch_new()
    subjects = {e.subject for e in out}
    assert "Invoice 42" not in subjects        # dropped by the custom function


def test_stages_modify_and_drop() -> None:
    def tag(email):                            # modify
        return email.model_copy(update={"subject": email.subject + " [seen]"})

    def drop_gmail(email):                     # drop (stage acting as a filter)
        return None if email.from_.address.endswith("@gmail.com") else email

    mf = connect("memory", seed=_seed(), tenant="acme", stages=[tag, drop_gmail])
    out = mf.fetch_new()
    assert all(e.subject.endswith("[seen]") for e in out)      # modified
    assert "bob@gmail.com" not in {e.from_.address for e in out}  # dropped by stage


def test_clean_fn_runs_first() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme",
                 clean_fn=lambda e: e.model_copy(update={"subject": "CLEANED"}))
    out = mf.fetch_new()
    assert all(e.subject == "CLEANED" for e in out)
