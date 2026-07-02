"""Internal seams so the adapter is testable without a real HTTP client / MSAL."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mailflow.core.errors import MailflowError


class GraphError(MailflowError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"graph error {status_code}: {message}")


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
