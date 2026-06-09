import pytest

from mailflow.core.errors import (
    ConfigError,
    ExtractionError,
    MailflowError,
    OversizedMessageError,
    UnknownKindError,
)


def test_all_errors_subclass_mailflow_error():
    for exc in (OversizedMessageError, ExtractionError, ConfigError, UnknownKindError):
        assert issubclass(exc, MailflowError)


def test_unknown_kind_is_a_config_error():
    assert issubclass(UnknownKindError, ConfigError)


def test_oversized_carries_the_byte_counts():
    err = OversizedMessageError(actual=200, limit=100)
    assert err.actual == 200 and err.limit == 100
    assert "200" in str(err) and "100" in str(err)
