"""Tests for AttachmentRule, AttachmentPolicy, and normalize_attachment_policy."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mailflow.extract.policy import (
    AttachmentPolicy,
    AttachmentRule,
    normalize_attachment_policy,
)
from mailflow.extract.streaming import MAX_ATTACHMENT_BYTES


# ---------------------------------------------------------------------------
# AttachmentRule
# ---------------------------------------------------------------------------


def test_rule_defaults() -> None:
    rule = AttachmentRule()
    assert rule.max_bytes == MAX_ATTACHMENT_BYTES
    assert rule.allowlist == frozenset()
    assert rule.scanner is None


def test_rule_max_bytes_zero_raises() -> None:
    with pytest.raises(ValidationError):
        AttachmentRule(max_bytes=0)


def test_rule_max_bytes_negative_raises() -> None:
    with pytest.raises(ValidationError):
        AttachmentRule(max_bytes=-1)


def test_rule_max_bytes_positive_ok() -> None:
    rule = AttachmentRule(max_bytes=1)
    assert rule.max_bytes == 1


def test_rule_allowlist_coerced_from_list() -> None:
    rule = AttachmentRule(allowlist=["a", "B"])
    assert rule.allowlist == frozenset({"a", "B"})


def test_rule_allowlist_coerced_from_set() -> None:
    rule = AttachmentRule(allowlist={"pdf", "image/png"})
    assert rule.allowlist == frozenset({"pdf", "image/png"})


def test_rule_model_validate_roundtrip() -> None:
    data: dict[str, Any] = {"max_bytes": 1024, "allowlist": ["pdf"]}
    rule = AttachmentRule.model_validate(data)
    assert rule.max_bytes == 1024
    assert rule.allowlist == frozenset({"pdf"})


def test_rule_scanner_accepts_protocol_impl() -> None:
    from mailflow.core.models import Attachment, ScanResult

    class _NoOp:
        def scan(self, attachment: Attachment) -> ScanResult:
            return ScanResult()

    rule = AttachmentRule(scanner=_NoOp())
    assert rule.scanner is not None


# ---------------------------------------------------------------------------
# AttachmentPolicy
# ---------------------------------------------------------------------------


def test_policy_defaults() -> None:
    policy = AttachmentPolicy()
    assert isinstance(policy.real, AttachmentRule)
    assert isinstance(policy.inline, AttachmentRule)
    assert policy.real.max_bytes == MAX_ATTACHMENT_BYTES
    assert policy.inline.max_bytes == MAX_ATTACHMENT_BYTES


def test_policy_real_and_inline_are_independent_instances() -> None:
    policy = AttachmentPolicy()
    assert policy.real is not policy.inline


def test_policy_model_validate_dict_with_sub_rules() -> None:
    data: dict[str, Any] = {
        "real": {"max_bytes": 100, "allowlist": ["pdf"]},
        "inline": {"max_bytes": 200},
    }
    policy = AttachmentPolicy.model_validate(data)
    assert policy.real.max_bytes == 100
    assert policy.real.allowlist == frozenset({"pdf"})
    assert policy.inline.max_bytes == 200


# ---------------------------------------------------------------------------
# normalize_attachment_policy
# ---------------------------------------------------------------------------


def test_normalize_none_returns_none() -> None:
    assert normalize_attachment_policy(None) is None


def test_normalize_policy_returns_itself() -> None:
    policy = AttachmentPolicy()
    result = normalize_attachment_policy(policy)
    assert result is policy


def test_normalize_rule_broadcasts_to_both() -> None:
    rule = AttachmentRule(max_bytes=512, allowlist=["pdf"])
    result = normalize_attachment_policy(rule)
    assert result is not None
    assert result.real is rule
    assert result.inline is rule


def test_normalize_dict_with_real_key() -> None:
    data: dict[str, Any] = {"real": {"max_bytes": 100}, "inline": {"max_bytes": 200}}
    result = normalize_attachment_policy(data)
    assert result is not None
    assert result.real.max_bytes == 100
    assert result.inline.max_bytes == 200


def test_normalize_dict_with_inline_key() -> None:
    data: dict[str, Any] = {"inline": {"max_bytes": 50}}
    result = normalize_attachment_policy(data)
    assert result is not None
    assert result.inline.max_bytes == 50
    # real uses default
    assert result.real.max_bytes == MAX_ATTACHMENT_BYTES


def test_normalize_bare_dict_broadcasts_to_both() -> None:
    data: dict[str, Any] = {"max_bytes": 9999, "allowlist": ["image/png"]}
    result = normalize_attachment_policy(data)
    assert result is not None
    assert result.real.max_bytes == 9999
    assert result.inline.max_bytes == 9999
    assert result.real.allowlist == frozenset({"image/png"})
    assert result.inline.allowlist == frozenset({"image/png"})
    # both should be the SAME object (broadcast)
    assert result.real is result.inline


def test_normalize_bare_dict_same_rule_on_both() -> None:
    data: dict[str, Any] = {"max_bytes": 1}
    result = normalize_attachment_policy(data)
    assert result is not None
    assert result.real is result.inline
