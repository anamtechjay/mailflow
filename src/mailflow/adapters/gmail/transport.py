"""Internal seams so the Gmail adapter is testable without a real HTTP client / OAuth.
Mirrors the Graph adapter's transport so the two stay independent (parallel adapters)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mailflow.core.errors import MailflowError


class GmailError(MailflowError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"gmail error {status_code}: {message}")


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
