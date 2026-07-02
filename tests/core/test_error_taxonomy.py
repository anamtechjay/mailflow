"""A2 — typed error taxonomy + provider errors re-parented under MailflowError.

Phase 0 contract: this is a *shape* test. Auth/Permanent/Transient must exist
and join the MailflowError tree; the provider transport errors must also subclass
MailflowError (CLAUDE.md: "every mailflow error subclasses MailflowError").
"""

from __future__ import annotations

from mailflow.adapters.gmail.transport import GmailError, StaleHistoryError
from mailflow.adapters.graph.transport import GraphError
from mailflow.core.errors import (
    AuthError,
    MailflowError,
    PermanentError,
    TransientError,
)


def test_taxonomy_classes_subclass_mailflow_error() -> None:
    for cls in (AuthError, PermanentError, TransientError):
        assert issubclass(cls, MailflowError)


def test_provider_errors_reparented_under_mailflow_error() -> None:
    for cls in (GraphError, GmailError, StaleHistoryError):
        assert issubclass(cls, MailflowError)


def test_provider_errors_keep_status_code() -> None:
    assert GraphError(404, "gone").status_code == 404
    assert GmailError(429, "slow down").status_code == 429
