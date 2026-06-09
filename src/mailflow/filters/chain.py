"""The ordered filter chain (spec §7.4). First KEEP or DROP wins; otherwise UNCERTAIN."""

from __future__ import annotations

from mailflow.core.filtering import FilterContext, FilterDecision
from mailflow.core.models import Decision, Envelope
from mailflow.core.ports import Filter


class FilterChain:
    def __init__(self, filters: list[Filter]) -> None:
        self.filters = filters

    def run(self, env: Envelope, ctx: FilterContext) -> FilterDecision:
        for f in self.filters:
            decision = f.evaluate(env, ctx)
            if decision.decision in (Decision.keep, Decision.drop):
                return decision
        return FilterDecision.uncertain()
