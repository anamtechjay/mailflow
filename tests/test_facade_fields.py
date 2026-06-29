"""End-to-end field selection through connect() (spec §4a)."""

from __future__ import annotations

from mailflow import connect
from mailflow.core.models import CleanEmail, StreamRef
from mailflow.providers.memory import SeedEmail

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")
RAW = (b"Message-ID: <m1@x>\r\nFrom: Alice <alice@partner.com>\r\n"
       b"To: ops@acme.com\r\nSubject: hello\r\n\r\nbody text")


def _seed() -> dict:
    return {STREAM: [SeedEmail("m1", RAW)]}


def test_default_yields_full_clean_email() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme")
    out = mf.fetch_new()
    assert isinstance(out[0], CleanEmail)


def test_fields_yields_only_selected_keys() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme", fields=["subject", "from"])
    out = mf.fetch_new()
    assert isinstance(out[0], dict)
    assert set(out[0].keys()) == {"subject", "from"}
    assert out[0]["subject"] == "hello"
    assert out[0]["from"].address == "alice@partner.com"


def test_fields_via_stream() -> None:
    mf = connect("memory", seed=_seed(), tenant="acme", fields=["subject"])
    got = list(mf.stream())
    assert got == [{"subject": "hello"}]


def test_fields_via_on_email_callback() -> None:
    seen: list = []
    mf = connect("memory", seed=_seed(), tenant="acme",
                 fields=["subject", "from"], on_email=seen.append)
    mf.run()
    assert seen[0]["subject"] == "hello"
    assert seen[0]["from"].address == "alice@partner.com"


def test_fields_with_filter() -> None:
    seed = {STREAM: [
        SeedEmail("m1", RAW),
        SeedEmail("m2", b"Message-ID: <m2@x>\r\nFrom: bob@gmail.com\r\n"
                        b"To: ops@acme.com\r\nSubject: hi\r\n\r\nx"),
    ]}
    mf = connect("memory", seed=seed, tenant="acme",
                 fields=["from"], filters=[{"kind": "no_personal"}])
    out = mf.fetch_new()
    assert [d["from"].address for d in out] == ["alice@partner.com"]  # gmail filtered + projected
