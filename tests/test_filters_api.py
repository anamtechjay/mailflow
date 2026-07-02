"""The unified filter API: FunctionFilter, NoPersonalFilter, and normalize_filters."""

from __future__ import annotations

from mailflow.core.filtering import FilterContext
from mailflow.core.models import Decision, Envelope, Recipient, StreamRef
from mailflow.filters.deterministic import (
    BlacklistFilter,
    FunctionFilter,
    NoPersonalFilter,
)

CTX = FilterContext(tenant="t")


def _env(address: str, subject: str = "") -> Envelope:
    return Envelope(
        canonical_id="c", provider="memory", provider_message_id="m",
        stream=StreamRef(mailbox="ops@acme.com"),
        from_=Recipient(address=address), subject=subject,
    )


# ---- FunctionFilter: True passes, False drops ----

def test_function_filter_true_passes() -> None:
    f = FunctionFilter(lambda env: True)
    assert f.evaluate(_env("a@x.com"), CTX).decision is Decision.uncertain


def test_function_filter_false_drops() -> None:
    f = FunctionFilter(lambda env: False)
    assert f.evaluate(_env("a@x.com"), CTX).decision is Decision.drop


def test_function_filter_uses_envelope() -> None:
    f = FunctionFilter(lambda env: "invoice" not in env.subject.lower())
    assert f.evaluate(_env("a@x.com", "Your invoice"), CTX).decision is Decision.drop
    assert f.evaluate(_env("a@x.com", "hello"), CTX).decision is Decision.uncertain


# ---- NoPersonalFilter: consumer domains dropped ----

def test_no_personal_drops_consumer_domains() -> None:
    f = NoPersonalFilter()
    for d in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com"):
        assert f.evaluate(_env(f"a@{d}"), CTX).decision is Decision.drop


def test_no_personal_passes_business_domain() -> None:
    f = NoPersonalFilter()
    assert f.evaluate(_env("a@techjays.com"), CTX).decision is Decision.uncertain


def test_no_personal_custom_domains() -> None:
    f = NoPersonalFilter(domains={"blocked.com"})
    assert f.evaluate(_env("a@blocked.com"), CTX).decision is Decision.drop
    assert f.evaluate(_env("a@gmail.com"), CTX).decision is Decision.uncertain  # not in override


# ---- normalize_filters: dict | function | Filter object ----

def test_normalize_filters_mixed() -> None:
    from mailflow.facade import normalize_filters

    out = normalize_filters([
        {"kind": "blacklist", "domains": ["spam.com"]},   # dict spec
        lambda env: True,                                  # function
        BlacklistFilter(domains={"x.com"}),                # Filter object
    ])
    assert len(out) == 3
    assert out[0].name == "blacklist"
    assert out[1].name == "custom"           # function -> FunctionFilter
    assert isinstance(out[2], BlacklistFilter)


def test_normalize_filters_empty_is_safe() -> None:
    from mailflow.facade import normalize_filters
    assert normalize_filters([]) == []
    assert normalize_filters(None) == []
