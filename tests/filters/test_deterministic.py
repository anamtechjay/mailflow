from mailflow.core.filtering import FilterContext
from mailflow.core.models import Decision, Envelope, Recipient, StreamRef
from mailflow.filters.deterministic import (
    BlacklistFilter,
    InternalDomainFilter,
    ListMailFilter,
    SubjectFilter,
    WhitelistFilter,
)

CTX = FilterContext(tenant="acme")


def _env(**kw) -> Envelope:
    base = dict(
        canonical_id="c",
        provider="memory",
        provider_message_id="m",
        stream=StreamRef(mailbox="ops@acme.com"),
    )
    base.update(kw)
    return Envelope(**base)


def test_whitelist_keeps_matching_domain_else_uncertain():
    f = WhitelistFilter(domains={"partner.com"})
    keep = f.evaluate(_env(**{"from": Recipient(address="x@partner.com")}), CTX)
    miss = f.evaluate(_env(**{"from": Recipient(address="x@other.com")}), CTX)
    assert keep.decision is Decision.keep
    assert miss.decision is Decision.uncertain


def test_blacklist_drops_matching_else_uncertain():
    f = BlacklistFilter(domains={"spam.com"})
    drop = f.evaluate(_env(**{"from": Recipient(address="x@spam.com")}), CTX)
    assert drop.decision is Decision.drop
    assert f.evaluate(_env(**{"from": Recipient(address="x@ok.com")}), CTX).decision is Decision.uncertain


def test_empty_destructive_filters_never_drop():
    # Opinionated default: empty config = no opinion (spec §7.4).
    for f in (BlacklistFilter(domains=set()), InternalDomainFilter(domains=set()),
              SubjectFilter(patterns=[])):
        d = f.evaluate(_env(subject="anything", **{"from": Recipient(address="x@y.com")}), CTX)
        assert d.decision is Decision.uncertain


def test_subject_filter_drops_on_regex_match():
    f = SubjectFilter(patterns=[r"(?i)out of office"])
    d = f.evaluate(_env(subject="Automatic reply: Out Of Office"), CTX)
    assert d.decision is Decision.drop


def test_list_mail_filter_drops_when_list_id_present():
    f = ListMailFilter()
    d = f.evaluate(_env(list_id="<news.x.com>"), CTX)
    assert d.decision is Decision.drop
    assert f.evaluate(_env(), CTX).decision is Decision.uncertain
