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
