"""Exception hierarchy. Every mailflow error subclasses MailflowError."""


class MailflowError(Exception):
    """Base for all mailflow errors."""


class ExtractionError(MailflowError):
    """Raised when a message cannot be parsed into a CleanEmail."""


class OversizedMessageError(MailflowError):
    """Raised when a message exceeds the configured byte ceiling (spec §8.6)."""

    def __init__(self, actual: int, limit: int) -> None:
        self.actual = actual
        self.limit = limit
        super().__init__(f"message is {actual} bytes, over the {limit}-byte limit")


class ConfigError(MailflowError):
    """Invalid configuration."""


class UnknownKindError(ConfigError):
    """A config `kind` is not in the registry (spec §11)."""


class AuthError(MailflowError):
    """Login/permission failure (e.g. 401). Routing: force one token refresh, retry the
    message exactly once, else DLQ — never a max_attempts loop (A2)."""


class PermanentError(MailflowError):
    """Will never succeed (403/404/410, invalid base64). Routing: DLQ, no retry (A2)."""


class TransientError(MailflowError):
    """Temporary failure (429/5xx/network). Routing: bounded backoff retry (A2)."""
