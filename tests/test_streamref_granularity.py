"""A12 — StreamRef granularity is provider-defined: the core leaves `folder` optional,
Gmail watches at mailbox level (folder=None); a per-folder provider (Graph) sets folder."""

from __future__ import annotations

from mailflow.adapters.gmail.provider import GmailProvider
from mailflow.core.models import StreamRef


def test_streamref_folder_is_optional_and_frozen() -> None:
    assert StreamRef(mailbox="a@x.com").folder is None
    assert StreamRef(mailbox="a@x.com", folder="Inbox").folder == "Inbox"
    # frozen: granularity, once chosen by the provider, is immutable
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        StreamRef(mailbox="a@x.com").folder = "X"  # type: ignore[misc]


def test_gmail_emits_one_mailbox_level_stream_per_watched_mailbox() -> None:
    provider = GmailProvider(client=object(), label_id="INBOX", tenant="t")  # type: ignore[arg-type]
    provider.submit("me@x.com", 100)
    streams = list(provider.sync_streams())
    assert streams == [StreamRef(mailbox="me@x.com", folder=None)]
    assert all(s.folder is None for s in streams)  # mailbox-level, never per-folder
