"""F04 — Identity (canonical_id / key) (unit + property). See docs/qa-partA-coverage.md."""

from __future__ import annotations

from mailflow.core.identity import derive_canonical_id, idempotency_key, stable_hash

PROVIDER = "memory"
PMID = "pmid-1"
MAILBOX = "me@acme.com"


def test_trusted_message_id_used():
    canonical_id, present, trusted = derive_canonical_id(
        provider=PROVIDER, provider_message_id=PMID, mailbox=MAILBOX,
        message_id="<abc@domain.com>",
    )
    assert canonical_id == "<abc@domain.com>"
    assert present is True
    assert trusted is True


def test_untrusted_falls_back_to_hash():
    message_id = "not a valid message id"  # no <>, contains spaces
    canonical_id, present, trusted = derive_canonical_id(
        provider=PROVIDER, provider_message_id=PMID, mailbox=MAILBOX,
        message_id=message_id,
    )
    assert trusted is False
    assert present is True  # it was present, just not trusted
    assert canonical_id == stable_hash(PROVIDER, PMID, MAILBOX)


def test_absent_message_id_hash():
    canonical_id, present, trusted = derive_canonical_id(
        provider=PROVIDER, provider_message_id=PMID, mailbox=MAILBOX, message_id=None,
    )
    assert present is False
    assert trusted is False
    assert canonical_id == stable_hash(PROVIDER, PMID, MAILBOX)


def test_same_id_diff_mailbox_diff_key():
    key1 = idempotency_key("acme", "mb1@acme.com", PMID)
    key2 = idempotency_key("acme", "mb2@acme.com", PMID)
    assert key1 != key2

    # A trusted Message-ID's canonical_id does not depend on mailbox at all.
    cid1, _, _ = derive_canonical_id(
        provider=PROVIDER, provider_message_id=PMID, mailbox="mb1@acme.com",
        message_id="<same@domain.com>",
    )
    cid2, _, _ = derive_canonical_id(
        provider=PROVIDER, provider_message_id=PMID, mailbox="mb2@acme.com",
        message_id="<same@domain.com>",
    )
    assert cid1 == cid2


def test_stable_hash_deterministic():
    h1 = stable_hash(PROVIDER, PMID, MAILBOX)
    h2 = stable_hash(PROVIDER, PMID, MAILBOX)
    assert h1 == h2
    # Sanity: changing any input changes the hash (it's not a constant).
    assert stable_hash("other", PMID, MAILBOX) != h1
    assert stable_hash(PROVIDER, "other", MAILBOX) != h1
    assert stable_hash(PROVIDER, PMID, "other@acme.com") != h1
