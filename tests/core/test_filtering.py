from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import Decision


def test_filter_decision_keep():
    d = FilterDecision.keep("whitelist", "domain partner.com")
    assert d.decision is Decision.keep
    assert d.filter_name == "whitelist"
    assert "partner.com" in d.reason


def test_filter_decision_uncertain_has_no_filter_name_requirement():
    d = FilterDecision.uncertain()
    assert d.decision is Decision.uncertain
    assert d.filter_name == ""


def test_filter_context_carries_tenant():
    ctx = FilterContext(tenant="acme")
    assert ctx.tenant == "acme"
