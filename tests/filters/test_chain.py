from mailflow.core.filtering import FilterContext
from mailflow.core.models import Decision, Envelope, Recipient, StreamRef
from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import BlacklistFilter, SubjectFilter, WhitelistFilter

CTX = FilterContext(tenant="acme")


def _env(addr="x@y.com", subject="") -> Envelope:
    return Envelope(
        canonical_id="c", provider="memory", provider_message_id="m",
        stream=StreamRef(mailbox="ops@acme.com"), subject=subject,
        **{"from": Recipient(address=addr)},
    )


def test_first_keep_short_circuits_and_stops():
    chain = FilterChain([
        WhitelistFilter(domains={"partner.com"}),
        SubjectFilter(patterns=[r".*"]),  # would DROP everything if reached
    ])
    d = chain.run(_env(addr="a@partner.com", subject="anything"), CTX)
    assert d.decision is Decision.keep
    assert d.filter_name == "whitelist"


def test_first_drop_short_circuits():
    chain = FilterChain([BlacklistFilter(domains={"spam.com"})])
    assert chain.run(_env(addr="a@spam.com"), CTX).decision is Decision.drop


def test_all_uncertain_yields_uncertain():
    chain = FilterChain([
        WhitelistFilter(domains={"partner.com"}),
        BlacklistFilter(domains={"spam.com"}),
    ])
    assert chain.run(_env(addr="a@neutral.com"), CTX).decision is Decision.uncertain


def test_empty_chain_is_uncertain():
    assert FilterChain([]).run(_env(), CTX).decision is Decision.uncertain
