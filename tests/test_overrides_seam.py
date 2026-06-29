"""A10 — overrides= seam + as_filter/as_cleaner marker decorators (Phase 0 contract).

Phase 0 freezes the *shapes*: connect()/build_from_config() accept an overrides=
mapping (accepted, not yet wired) and the two marker decorators exist and tag a
callable while returning it unchanged. Wiring lands in Phase 1.
"""

from __future__ import annotations

from mailflow import MailflowConfig, as_cleaner, as_filter, build_from_config, connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

RAW = b"Message-ID: <m1@x>\r\nFrom: a@x.com\r\nTo: b@y.com\r\nSubject: hi\r\n\r\nbody"
STREAM = StreamRef(mailbox="b@y.com", folder="Inbox")


def test_connect_accepts_overrides_none() -> None:
    mf = connect("memory", seed={STREAM: [SeedEmail("m1", RAW)]}, overrides=None, tenant="t")
    assert mf.fetch_new()[0].subject == "hi"


def test_build_from_config_accepts_overrides() -> None:
    pipe = build_from_config(MailflowConfig(), overrides=None)
    assert pipe is not None


def test_as_filter_marks_callable_and_returns_it() -> None:
    @as_filter
    def f(env: object) -> bool:
        return True

    assert f.__mailflow_role__ == "filter"
    assert f("anything") is True


def test_as_cleaner_marks_callable_and_returns_it() -> None:
    @as_cleaner
    def c(email: object) -> object:
        return email

    assert c.__mailflow_role__ == "cleaner"
    sentinel = object()
    assert c(sentinel) is sentinel
