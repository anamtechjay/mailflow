from mailflow.core.identity import (
    derive_canonical_id,
    idempotency_key,
    is_message_id_trusted,
    stable_hash,
)


def test_idempotency_key_combines_tenant_mailbox_provider_id():
    assert idempotency_key("acme", "ops@x.com", "m1") == "acme|ops@x.com|m1"


def test_idempotency_key_is_stable_and_distinct():
    a = idempotency_key("acme", "ops@x.com", "m1")
    b = idempotency_key("acme", "ops@x.com", "m1")
    c = idempotency_key("acme", "other@x.com", "m1")  # same id, different mailbox -> distinct
    assert a == b and a != c


def test_stable_hash_is_deterministic_and_provider_scoped():
    h1 = stable_hash("graph", "pmid", "ops@x.com")
    h2 = stable_hash("graph", "pmid", "ops@x.com")
    h3 = stable_hash("gmail", "pmid", "ops@x.com")
    assert h1 == h2 and h1 != h3
    assert len(h1) == 64  # sha256 hex


def test_present_well_formed_message_id_is_trusted():
    assert is_message_id_trusted("<abc.123@example.com>") is True


def test_absent_or_malformed_message_id_is_not_trusted():
    assert is_message_id_trusted(None) is False
    assert is_message_id_trusted("") is False
    assert is_message_id_trusted("not-an-id") is False  # no @, no angle brackets


def test_canonical_id_uses_message_id_when_trusted():
    cid, present, trusted = derive_canonical_id(
        provider="graph",
        provider_message_id="pmid",
        mailbox="ops@x.com",
        message_id="<abc.123@example.com>",
    )
    assert cid == "<abc.123@example.com>"
    assert present is True and trusted is True


def test_canonical_id_falls_back_to_stable_hash_when_untrusted():
    cid, present, trusted = derive_canonical_id(
        provider="graph",
        provider_message_id="pmid",
        mailbox="ops@x.com",
        message_id=None,
    )
    assert cid == stable_hash("graph", "pmid", "ops@x.com")
    assert present is False and trusted is False
