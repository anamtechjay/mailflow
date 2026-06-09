import json

from mailflow.core.events import EmailEvent
from mailflow.core.models import CleanEmail, Recipient
from mailflow.emit.memory import EmitReceipt, MemoryEmitter
from mailflow.emit.stdout import StdoutEmitter


def _event() -> EmailEvent:
    return EmailEvent(
        tenant="acme",
        ordering_key="ops@acme.com",
        email=CleanEmail(
            canonical_id="c1", provider="memory", provider_message_id="m1",
            provider_stream_id="s", **{"from": Recipient(address="a@x.com")},
        ),
    )


def test_memory_emitter_collects_events_and_returns_receipt():
    em = MemoryEmitter()
    receipt = em.emit(_event())
    assert isinstance(receipt, EmitReceipt)
    assert receipt.accepted is True
    assert em.events[0].email.canonical_id == "c1"


def test_stdout_emitter_writes_json_line(capsys):
    StdoutEmitter().emit(_event())
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["email"]["canonical_id"] == "c1"
    assert parsed["schema_version"] == "1.0"
