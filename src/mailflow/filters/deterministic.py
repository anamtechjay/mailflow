"""Deterministic, three-valued filters (spec §7.4). Destructive filters with empty
config return UNCERTAIN so a reusable toolkit never silently drops real mail."""

from __future__ import annotations

import re

from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import Envelope


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


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


class ListMailFilter:
    """Drops mailing-list / auto-submitted mail using free header signals (§7.4, R-D3)."""

    name = "list_mail"

    def evaluate(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        if env.list_id or (env.auto_submitted and env.auto_submitted.lower() != "no"):
            return FilterDecision.drop(self.name, "list/auto-submitted header present")
        return FilterDecision.uncertain()
