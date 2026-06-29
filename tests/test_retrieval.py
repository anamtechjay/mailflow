"""Retrieve-by-message-ID helpers on the Mailflow handle (spec §4)."""

from __future__ import annotations

import pytest

from mailflow.core.models import Attachment, CleanEmail, Recipient
from mailflow.emit.memory import MemoryEmitter
from mailflow.facade import Mailflow
from mailflow.stores.memory import InMemoryCursorStore


def _fake_email(mid: str) -> CleanEmail:
    return CleanEmail(
        canonical_id="c", provider="gmail", provider_message_id=mid,
        provider_stream_id="s", subject="hi", body_text="hello body",
        to=[Recipient(address="x@acme.com")],
        attachments=[Attachment(filename="f.pdf", size_bytes=10)],
    )


def _handle(fetcher=None) -> Mailflow:
    return Mailflow(
        provider_kind="gmail" if fetcher else "memory",
        emitter=MemoryEmitter(), cursor_store=InMemoryCursorStore(), fetcher=fetcher,
    )


def test_get_email() -> None:
    mf = _handle(fetcher=_fake_email)
    e = mf.get_email("m1")
    assert isinstance(e, CleanEmail) and e.provider_message_id == "m1" and e.subject == "hi"


def test_get_body() -> None:
    assert _handle(fetcher=_fake_email).get_body("m1") == "hello body"


def test_get_recipients() -> None:
    recips = _handle(fetcher=_fake_email).get_recipients("m1")
    assert [r.address for r in recips] == ["x@acme.com"]


def test_get_attachments() -> None:
    atts = _handle(fetcher=_fake_email).get_attachments("m1")
    assert [a.filename for a in atts] == ["f.pdf"]


def test_get_email_without_fetcher_raises() -> None:
    with pytest.raises(NotImplementedError):
        _handle(fetcher=None).get_email("m1")
