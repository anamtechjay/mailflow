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
