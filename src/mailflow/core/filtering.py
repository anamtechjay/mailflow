"""Shared filter value types (kept out of ports.py to avoid import cycles)."""

from __future__ import annotations

from pydantic import BaseModel

from mailflow.core.models import Decision


class FilterContext(BaseModel):
    tenant: str = ""


class FilterDecision(BaseModel):
    decision: Decision
    filter_name: str = ""
    reason: str = ""

    @classmethod
    def keep(cls, filter_name: str, reason: str = "") -> "FilterDecision":
        return cls(decision=Decision.keep, filter_name=filter_name, reason=reason)

    @classmethod
    def drop(cls, filter_name: str, reason: str = "") -> "FilterDecision":
        return cls(decision=Decision.drop, filter_name=filter_name, reason=reason)

    @classmethod
    def uncertain(cls) -> "FilterDecision":
        return cls(decision=Decision.uncertain)
