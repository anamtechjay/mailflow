"""Field selection / projection helper (spec §4a)."""

from __future__ import annotations

import pytest

from mailflow.core.models import Attachment, CleanEmail, Recipient
from mailflow.facade import make_projection


def _email() -> CleanEmail:
    return CleanEmail(
        canonical_id="c", provider="gmail", provider_message_id="m1",
        provider_stream_id="s", subject="hi", body_text="body",
        from_=Recipient(name="A", address="a@x.com"),
        to=[Recipient(address="ops@acme.com")],
        attachments=[Attachment(filename="f.pdf", size_bytes=10)],
    )


def test_selects_only_requested_fields() -> None:
    project = make_projection(["subject", "body_text"])
    out = project(_email())
    assert out == {"subject": "hi", "body_text": "body"}


def test_from_alias_returns_recipient() -> None:
    out = make_projection(["from"])(_email())
    assert "from" in out and out["from"].address == "a@x.com"


def test_attachments_field() -> None:
    out = make_projection(["attachments"])(_email())
    assert [a.filename for a in out["attachments"]] == ["f.pdf"]


def test_unknown_field_raises() -> None:
    with pytest.raises(ValueError):
        make_projection(["subject", "bogus_field"])


def test_empty_fields_returns_empty_dict() -> None:
    assert make_projection([])(_email()) == {}
