"""Built-in To/Cc filters: exact-address and regex matching, drop-on-match, empty=no-op."""

from __future__ import annotations

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Decision, Envelope, Recipient, StreamRef
from mailflow.filters.deterministic import CcFilter, ToFilter

CTX = FilterContext(tenant="t")
STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _env(to: tuple[str, ...] = (), cc: tuple[str, ...] = ()) -> Envelope:
    return Envelope(
        canonical_id="c", provider="memory", provider_message_id="m",
        stream=STREAM, from_=Recipient(address="a@x.com"),
        to=[Recipient(address=a) for a in to],
        cc=[Recipient(address=a) for a in cc],
    )


def test_to_filter_exact_address_drops() -> None:
    f = ToFilter(addresses={"alerts@acme.com"})
    assert f.evaluate(_env(to=("alerts@acme.com",)), CTX).decision is Decision.drop
    assert f.evaluate(_env(to=("real@acme.com",)), CTX).decision is Decision.uncertain


def test_to_filter_case_insensitive() -> None:
    f = ToFilter(addresses={"Alerts@Acme.com"})
    assert f.evaluate(_env(to=("ALERTS@acme.COM",)), CTX).decision is Decision.drop


def test_to_filter_regex_pattern_drops() -> None:
    f = ToFilter(patterns=[r"(?i)^noreply@"])
    assert f.evaluate(_env(to=("noreply@vendor.io",)), CTX).decision is Decision.drop
    assert f.evaluate(_env(to=("sales@vendor.io",)), CTX).decision is Decision.uncertain


def test_to_filter_empty_is_safe_noop() -> None:
    assert ToFilter().evaluate(_env(to=("anyone@acme.com",)), CTX).decision is Decision.uncertain


def test_cc_filter_matches_cc_not_to() -> None:
    f = CcFilter(addresses={"list@acme.com"})
    assert f.evaluate(_env(cc=("list@acme.com",)), CTX).decision is Decision.drop
    assert f.evaluate(_env(to=("list@acme.com",)), CTX).decision is Decision.uncertain


def test_cc_filter_regex() -> None:
    f = CcFilter(patterns=[r"@bulk\.example$"])
    assert f.evaluate(_env(cc=("x@bulk.example",)), CTX).decision is Decision.drop
    assert f.evaluate(_env(cc=("x@real.example",)), CTX).decision is Decision.uncertain


# ---- end-to-end through connect() (config dict spec; flat + nested params) ----

from mailflow import connect  # noqa: E402
from mailflow.providers.memory import SeedEmail  # noqa: E402


def _raw(mid: str, to: str, cc: str = "") -> bytes:
    cc_line = f"Cc: {cc}\r\n" if cc else ""
    return (f"Message-ID: <{mid}@x>\r\nFrom: s@x.com\r\nTo: {to}\r\n{cc_line}"
            f"Subject: hi\r\n\r\nbody").encode()


def test_to_filter_e2e_drops_matching_recipient() -> None:
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", "alerts@acme.com")),
        SeedEmail("m2", _raw("m2", "real@acme.com")),
    ]}
    mf = connect("memory", seed=seed, tenant="acme",
                 filters=[{"kind": "to", "addresses": ["alerts@acme.com"]}])
    out = mf.fetch_new()
    assert {r.address for e in out for r in e.to} == {"real@acme.com"}


def test_cc_filter_e2e_regex_nested_params() -> None:
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", "real@acme.com", cc="bulk@lists.io")),
        SeedEmail("m2", _raw("m2", "real@acme.com", cc="ok@acme.com")),
    ]}
    mf = connect("memory", seed=seed, tenant="acme",
                 filters=[{"kind": "cc", "params": {"patterns": ["@lists\\.io$"]}}])
    out = mf.fetch_new()
    assert len(out) == 1
    assert out[0].cc[0].address == "ok@acme.com"
