from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail, Recipient


def _email() -> CleanEmail:
    return CleanEmail(
        canonical_id="c",
        provider="memory",
        provider_message_id="m1",
        provider_stream_id="s",
        from_=Recipient(address="a@x.com"),
    )


def test_event_stamps_the_schema_version():
    evt = EmailEvent(email=_email(), tenant="acme", ordering_key="ops@x.com")
    assert evt.schema_version == SCHEMA_VERSION
    assert SCHEMA_VERSION.count(".") == 1  # major.minor (spec §14)


def test_event_carries_tenant_and_ordering_key():
    evt = EmailEvent(email=_email(), tenant="acme", ordering_key="ops@x.com")
    assert evt.tenant == "acme"
    assert evt.ordering_key == "ops@x.com"
