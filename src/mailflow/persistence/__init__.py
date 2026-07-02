"""Downstream persistence for received CleanEmails (consuming-app side)."""

from mailflow.persistence.sqlite_store import SqliteEmailStore

__all__ = ["SqliteEmailStore"]
