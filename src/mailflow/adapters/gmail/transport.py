"""Internal seams so the Gmail adapter is testable without a real HTTP client / OAuth.
Mirrors the Graph adapter's transport so the two stay independent (parallel adapters)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mailflow.core.errors import (
    AuthError,
    MailflowError,
    PermanentError,
    TransientError,
)


class GmailError(MailflowError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"gmail error {status_code}: {message}")


# Typed Gmail errors (A2). Each carries the HTTP status (via GmailError) AND participates
# in the core routing taxonomy (Auth/Permanent/Transient) via multiple inheritance, so:
#   - callers route on isinstance(exc, AuthError|PermanentError|TransientError)
#   - the existing `except GmailError` in history_message_ids still catches them, keeping
#     the history-404 -> StaleHistoryError special case working unchanged.
class GmailAuthError(AuthError, GmailError):
    """401 — refresh-and-retry-once candidate."""


class GmailPermanentError(PermanentError, GmailError):
    """403/404/410 — DLQ, no retry."""


class GmailTransientError(TransientError, GmailError):
    """429/5xx/network — bounded backoff retry."""


def gmail_error_for(status_code: int, message: str) -> GmailError:
    """Map an HTTP status to the right typed Gmail error; other 4xx -> plain GmailError."""
    if status_code == 401:
        return GmailAuthError(status_code, message)
    if status_code in (403, 404, 410):
        return GmailPermanentError(status_code, message)
    if status_code == 429 or status_code >= 500:
        return GmailTransientError(status_code, message)
    return GmailError(status_code, message)


class StaleHistoryError(MailflowError):
    """Gmail history.list returned 404 — the stored historyId is too old to diff from.
    Recovery: re-seed the cursor to the current historyId (provider self-heal)."""


@runtime_checkable
class HttpResponse(Protocol):
    status_code: int
    def json(self) -> Any: ...
    @property
    def headers(self) -> dict[str, str]: ...
    @property
    def content(self) -> bytes: ...


@runtime_checkable
class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, *, headers: dict[str, str], json: Any | None
    ) -> HttpResponse: ...


@runtime_checkable
class TokenProvider(Protocol):
    def get_token(self) -> str: ...
