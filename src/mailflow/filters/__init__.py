"""Built-in filters (spec §3.2). Import these for the object form, e.g.
`from mailflow.filters import BlacklistFilter`."""

from mailflow.filters.chain import FilterChain
from mailflow.filters.deterministic import (
    BlacklistFilter,
    BlockSenderFilter,
    FunctionFilter,
    InternalDomainFilter,
    ListMailFilter,
    NoPersonalFilter,
    OnlyDomainFilter,
    OnlySenderFilter,
    SubjectFilter,
    WhitelistFilter,
)

__all__ = [
    "FilterChain",
    "BlacklistFilter",
    "BlockSenderFilter",
    "WhitelistFilter",
    "OnlyDomainFilter",
    "OnlySenderFilter",
    "InternalDomainFilter",
    "SubjectFilter",
    "ListMailFilter",
    "NoPersonalFilter",
    "FunctionFilter",
]
