"""Full pipeline over the memory provider with a realistic mix: a keep (whitelist),
a drop (blacklist), an oversized (DLQ), a newsletter (list_mail drop), and a plain
uncertain->emit. Proves the §8 invariants hold together end-to-end with zero setup."""

from mailflow.builder import build_from_config
from mailflow.config.schema import MailflowConfig
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _raw(mid, frm="x@neutral.com", subject="hi", extra="", body="hello"):
    return (
        f"Message-ID: <{mid}@x>\r\nFrom: {frm}\r\nTo: ops@acme.com\r\n"
        f"Subject: {subject}\r\n{extra}\r\n{body}\r\n"
    ).encode()


def test_full_mixed_run():
    cfg = MailflowConfig.model_validate({
        "tenant": "acme",
        "provider": {"kind": "memory"},
        "filters": [
            {"kind": "whitelist", "params": {"domains": ["partner.com"]}},
            {"kind": "blacklist", "params": {"domains": ["spam.com"]}},
            {"kind": "list_mail"},
        ],
        "emitter": {"kind": "memory"},
        "max_message_bytes": 500,
    })
    seed = {STREAM: [
        SeedEmail("keep", _raw("keep", frm="vip@partner.com")),          # whitelist KEEP
        SeedEmail("drop", _raw("drop", frm="bad@spam.com")),             # blacklist DROP
        SeedEmail("news", _raw("news", extra="List-Id: <n.x.com>\r\n")), # list_mail DROP
        SeedEmail("big", _raw("big", body="z" * 600)),                   # oversized -> DLQ
        SeedEmail("plain", _raw("plain", frm="someone@acme-customer.com")),  # uncertain -> EMIT
    ]}
    pipe = build_from_config(cfg, seed=seed)
    report = pipe.run_once()

    assert report.fetched == 5
    assert report.emitted == 2          # keep + plain
    assert report.dropped == 2          # blacklist + list_mail
    assert report.dead_lettered == 1    # oversized
    # cursor advanced past every message (all reached a terminal disposition)
    assert pipe.cursor_store.get("acme", STREAM).order == 5
    # every message produced a decision trace (debuggability, §12)
    assert len(report.traces) == 5
