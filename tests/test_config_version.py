"""A11: validate() fails fast on a config-schema version whose major exceeds the
supported major (1), or on a malformed version string."""

from __future__ import annotations

import pytest

from mailflow.config.loader import validate
from mailflow.config.schema import MailflowConfig
from mailflow.core.errors import ConfigError


def test_future_major_version_rejected() -> None:
    with pytest.raises(ConfigError):
        validate(MailflowConfig(version="2.0"))


def test_malformed_version_rejected() -> None:
    with pytest.raises(ConfigError):
        validate(MailflowConfig(version="abc"))


def test_supported_versions_accepted() -> None:
    validate(MailflowConfig(version="1.0"))
    validate(MailflowConfig(version="1.5"))
