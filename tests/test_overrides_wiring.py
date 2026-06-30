"""A10: the overrides= mapping substitutes components by role key (provider | emitter |
cursor_store | dedupe_store | blob_store | cleaner | verifier) BEFORE the Pipeline is
built — in both facade.connect and builder.build_from_config."""

from __future__ import annotations

from mailflow import build_from_config, connect
from mailflow.config.schema import MailflowConfig
from mailflow.core.events import EmailEvent
from mailflow.core.models import CleanEmail, StreamRef
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryCursorStore

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")
RAW = (b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\n"
       b"To: ops@acme.com\r\nSubject: hi\r\n\r\nbody")


def _seed() -> dict[StreamRef, list[SeedEmail]]:
    return {STREAM: [SeedEmail("m1", RAW)]}


class SentinelEmitter:
    def __init__(self) -> None:
        self.events: list[EmailEvent] = []

    def emit(self, event: EmailEvent) -> object:
        self.events.append(event)
        return None


class TagCleaner:
    """A ContentCleaner that stamps a marker so we can prove it ran."""

    def clean(self, email: CleanEmail) -> CleanEmail:
        return email.model_copy(update={"subject": "CLEANED"})


def test_builder_overrides_emitter_and_cursor_store() -> None:
    sentinel = SentinelEmitter()
    cur = InMemoryCursorStore()
    pipe = build_from_config(
        MailflowConfig(tenant="acme"),
        seed=_seed(),
        overrides={"emitter": sentinel, "cursor_store": cur},
    )
    pipe.run_once()
    assert len(sentinel.events) == 1                       # override emitter received the event
    assert pipe.cursor_store is cur                        # override cursor store wired in
    assert cur.get("acme", STREAM) is not None


def test_builder_overrides_cleaner() -> None:
    sentinel = SentinelEmitter()
    pipe = build_from_config(
        MailflowConfig(tenant="acme"),
        seed=_seed(),
        overrides={"emitter": sentinel, "cleaner": TagCleaner()},
    )
    pipe.run_once()
    assert sentinel.events[0].email.subject == "CLEANED"


def test_overrides_inject_dlq_store_and_auth_refresher() -> None:
    from mailflow.stores.memory import InMemoryDeadLetterStore

    class _Refresher:
        def force_refresh(self) -> None:
            return None

    dlq_store = InMemoryDeadLetterStore()
    refresher = _Refresher()
    pipe = build_from_config(
        MailflowConfig(tenant="acme"),
        seed=_seed(),
        overrides={"dlq_store": dlq_store, "auth_refresher": refresher},
    )
    assert pipe.dlq_store is dlq_store
    assert pipe.auth_refresher is refresher


def test_connect_overrides_emitter() -> None:
    sentinel = SentinelEmitter()
    mf = connect("memory", seed=_seed(), tenant="acme", overrides={"emitter": sentinel})
    mf.run()
    assert len(sentinel.events) == 1


def test_connect_overrides_cleaner() -> None:
    sentinel = SentinelEmitter()
    mf = connect(
        "memory", seed=_seed(), tenant="acme",
        overrides={"emitter": sentinel, "cleaner": TagCleaner()},
    )
    mf.run()
    assert sentinel.events[0].email.subject == "CLEANED"
