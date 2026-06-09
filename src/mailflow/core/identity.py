"""Identity derivation (spec §6.1).

Two keys for two jobs:
  - idempotency_key((tenant, mailbox, provider_message_id)) -> dedupe (§8.2)
  - canonical_id -> always-present surrogate; uses RFC Message-ID only when trusted.
"""

from __future__ import annotations

import hashlib

_SEP = "|"


def idempotency_key(tenant: str, mailbox: str, provider_message_id: str) -> str:
    """Stable dedupe key. (tenant, mailbox) included so the same mail in two watched
    inboxes counts as two arrivals, not one (spec §6.1)."""
    return _SEP.join((tenant, mailbox, provider_message_id))


def stable_hash(provider: str, provider_message_id: str, mailbox: str) -> str:
    """Deterministic sha256 surrogate used when the Message-ID is untrusted."""
    raw = _SEP.join((provider, provider_message_id, mailbox)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def is_message_id_trusted(message_id: str | None) -> bool:
    """A Message-ID is trustworthy only if present and shaped like `<local@domain>`.
    RFC 5322 says it SHOULD (not MUST) exist and uniqueness is the sender's job, so
    we treat anything malformed as untrusted (spec §6.1, R-D1)."""
    if not message_id:
        return False
    mid = message_id.strip()
    return mid.startswith("<") and mid.endswith(">") and "@" in mid


def derive_canonical_id(
    *,
    provider: str,
    provider_message_id: str,
    mailbox: str,
    message_id: str | None,
) -> tuple[str, bool, bool]:
    """Return (canonical_id, message_id_present, message_id_trusted)."""
    present = bool(message_id and message_id.strip())
    trusted = is_message_id_trusted(message_id)
    if trusted:
        assert message_id is not None
        return message_id.strip(), present, trusted
    return stable_hash(provider, provider_message_id, mailbox), present, trusted
