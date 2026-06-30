"""Deterministic, three-valued filters (spec §7.4). Destructive filters with empty
config return UNCERTAIN so a reusable toolkit never silently drops real mail."""

from __future__ import annotations

import re
from typing import Callable

from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import Envelope, Recipient


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def _recipient_match(
    recipients: list[Recipient],
    addresses: set[str],
    patterns: list[re.Pattern[str]],
) -> str | None:
    """Reason string if any recipient matches an exact address (case-insensitive) or a
    regex, else None. Shared by ToFilter/CcFilter; mirrors SubjectFilter's regex search."""
    for r in recipients:
        addr = r.address.lower()
        if addr in addresses:
            return f"address {addr}"
        for pat in patterns:
            if pat.search(r.address):
                return f"~ {pat.pattern}"
    return None


# Consumer/personal mail domains blocked by the no_personal filter (spec §3.2).
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "hotmail.com",
    "outlook.com", "live.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com",
}


class WhitelistFilter:
    name = "whitelist"

    def __init__(self, domains: set[str]) -> None:
        self.domains = {d.lower() for d in domains}

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if self.domains and _domain(env.from_.address) in self.domains:
            return FilterDecision.keep(self.name, f"domain {_domain(env.from_.address)}")
        return FilterDecision.uncertain()


class BlacklistFilter:
    name = "blacklist"

    def __init__(self, domains: set[str]) -> None:
        self.domains = {d.lower() for d in domains}

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if self.domains and _domain(env.from_.address) in self.domains:
            return FilterDecision.drop(self.name, f"domain {_domain(env.from_.address)}")
        return FilterDecision.uncertain()


class OnlyDomainFilter:
    """Allow ONLY mail from the given domains — drops everything else (spec §3.2).
    Unlike `whitelist` (which only KEEPS matches and lets others fall through),
    this DROPS any sender whose domain is not in the set. Empty set = no-op (safe)."""

    name = "only_domain"

    def __init__(self, domains: set[str]) -> None:
        self.domains = {d.lower() for d in domains}

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if not self.domains:
            return FilterDecision.uncertain()                 # empty -> safe no-op
        if _domain(env.from_.address) in self.domains:
            return FilterDecision.uncertain()                 # allowed -> pass on
        return FilterDecision.drop(self.name, "domain not allowed")


class OnlySenderFilter:
    """Allow ONLY mail from specific people (exact email addresses) — drops all else.
    Person-level counterpart to `only_domain`. Empty set = no-op (safe)."""

    name = "only_sender"

    def __init__(self, addresses: set[str]) -> None:
        self.addresses = {a.lower() for a in addresses}

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if not self.addresses:
            return FilterDecision.uncertain()                 # empty -> safe no-op
        if env.from_.address.lower() in self.addresses:
            return FilterDecision.uncertain()                 # allowed person -> pass on
        return FilterDecision.drop(self.name, "sender not allowed")


class BlockSenderFilter:
    """Drop mail from specific people (exact email addresses) — the address-level
    counterpart to `blacklist` (which works on whole domains). Empty set = no-op (safe).
    Use this to block one person without blocking their whole domain."""

    name = "block_sender"

    def __init__(self, addresses: set[str]) -> None:
        self.addresses = {a.lower() for a in addresses}

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if env.from_.address.lower() in self.addresses:
            return FilterDecision.drop(self.name, "sender blocked")
        return FilterDecision.uncertain()


class InternalDomainFilter:
    name = "internal_domain"

    def __init__(self, domains: set[str]) -> None:
        self.domains = {d.lower() for d in domains}

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if self.domains and _domain(env.from_.address) in self.domains:
            return FilterDecision.drop(self.name, "internal domain")
        return FilterDecision.uncertain()


class SubjectFilter:
    name = "subject"

    def __init__(self, patterns: list[str]) -> None:
        self.patterns = [re.compile(p) for p in patterns]

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        for pat in self.patterns:
            if pat.search(env.subject):
                return FilterDecision.drop(self.name, f"subject ~ {pat.pattern}")
        return FilterDecision.uncertain()


class ToFilter:
    """Drops mail addressed TO a matching recipient — exact address (case-insensitive)
    or regex pattern, mirroring SubjectFilter's regex style. Empty config = no-op (safe)."""

    name = "to"

    def __init__(
        self, addresses: set[str] | None = None, patterns: list[str] | None = None
    ) -> None:
        self.addresses = {a.lower() for a in (addresses or set())}
        self.patterns = [re.compile(p) for p in (patterns or [])]

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if not self.addresses and not self.patterns:
            return FilterDecision.uncertain()
        reason = _recipient_match(env.to, self.addresses, self.patterns)
        if reason is not None:
            return FilterDecision.drop(self.name, reason)
        return FilterDecision.uncertain()


class CcFilter:
    """Cc counterpart of ToFilter — matches against the Cc list. Empty config = no-op (safe)."""

    name = "cc"

    def __init__(
        self, addresses: set[str] | None = None, patterns: list[str] | None = None
    ) -> None:
        self.addresses = {a.lower() for a in (addresses or set())}
        self.patterns = [re.compile(p) for p in (patterns or [])]

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if not self.addresses and not self.patterns:
            return FilterDecision.uncertain()
        reason = _recipient_match(env.cc, self.addresses, self.patterns)
        if reason is not None:
            return FilterDecision.drop(self.name, reason)
        return FilterDecision.uncertain()


class ListMailFilter:
    """Drops mailing-list / auto-submitted mail using free header signals (§7.4, R-D3)."""

    name = "list_mail"

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if env.list_id or env.is_auto_submitted:
            return FilterDecision.drop(self.name, "list/auto-submitted header present")
        return FilterDecision.uncertain()


class NoPersonalFilter:
    """Drops mail from consumer/personal domains (gmail/yahoo/…) — spec §3.2.
    Pass `domains` to override the default consumer set."""

    name = "no_personal"

    def __init__(self, domains: set[str] | None = None) -> None:
        self.domains = {d.lower() for d in domains} if domains else set(_PERSONAL_DOMAINS)

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if _domain(env.from_.address) in self.domains:
            return FilterDecision.drop(self.name, "personal domain")
        return FilterDecision.uncertain()


class FunctionFilter:
    """Wraps a user function `fn(env) -> bool` as a Filter (spec §3.3).
    `True` passes the email to the next filter; `False` drops it."""

    name = "custom"

    def __init__(self, fn: Callable[[Envelope], bool]) -> None:
        self._fn = fn

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        return FilterDecision.uncertain() if self._fn(env) else \
            FilterDecision.drop(self.name, "custom function")
