"""User-facing attachment policy models and normalization helper.

`AttachmentRule` captures the per-class (real / inline) policy knobs:
  - `max_bytes`  — hard per-attachment byte cap (> 0)
  - `allowlist`  — frozenset of allowed content-types / file extensions
  - `scanner`    — optional AttachmentScanner Protocol implementation

`AttachmentPolicy` pairs a rule for real attachments with one for inline parts.

`normalize_attachment_policy` accepts the full range of caller-supplied inputs
(None, a policy, a single rule, or a raw dict) and returns a canonical
`AttachmentPolicy | None` so the extractor always works with the same type.
"""

from __future__ import annotations

from typing import Any, Iterable, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mailflow.core.ports import AttachmentScanner
from mailflow.extract.streaming import MAX_ATTACHMENT_BYTES


class AttachmentRule(BaseModel):
    """Per-class attachment policy knobs (one for real, one for inline)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    max_bytes: int = MAX_ATTACHMENT_BYTES
    allowlist: frozenset[str] = frozenset()
    scanner: AttachmentScanner | None = None

    @field_validator("max_bytes")
    @classmethod
    def _max_bytes_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_bytes must be > 0")
        return v

    @field_validator("allowlist", mode="before")
    @classmethod
    def _coerce_allowlist(cls, v: object) -> frozenset[str]:
        if isinstance(v, frozenset):
            return v
        # Treat any other iterable (list, set, tuple, generator, …) as a sequence of strings.
        return frozenset(cast(Iterable[str], v))


class AttachmentPolicy(BaseModel):
    """Pairs per-class rules: one for real (disposition=attachment) parts and one
    for inline parts (CID-referenced images, etc.)."""

    real: AttachmentRule = Field(default_factory=AttachmentRule)
    inline: AttachmentRule = Field(default_factory=AttachmentRule)


def normalize_attachment_policy(
    value: AttachmentPolicy | AttachmentRule | dict[str, Any] | None,
) -> AttachmentPolicy | None:
    """Normalize any caller-supplied attachment-policy input to ``AttachmentPolicy | None``.

    Branches:
    - ``None``                             → ``None``
    - ``AttachmentPolicy``                 → itself (pass-through)
    - ``AttachmentRule``                   → ``AttachmentPolicy(real=v, inline=v)``
    - ``dict`` with ``"real"`` or ``"inline"`` key → ``AttachmentPolicy.model_validate(v)``
    - bare ``dict`` (no real/inline key)   → ``r = AttachmentRule.model_validate(v);
                                              AttachmentPolicy(real=r, inline=r)``
    """
    if value is None:
        return None
    if isinstance(value, AttachmentPolicy):
        return value
    if isinstance(value, AttachmentRule):
        return AttachmentPolicy(real=value, inline=value)
    # dict branches
    if "real" in value or "inline" in value:
        return AttachmentPolicy.model_validate(value)
    r = AttachmentRule.model_validate(value)
    return AttachmentPolicy(real=r, inline=r)
