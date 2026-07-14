"""Locks the raw-message correspondence bug fix in live.py: _ReceiverAdapter must
unwrap _MessageAdapter.raw before calling into the (fake, here) azure receiver.
Uses fakes only -- must NOT import azure-servicebus."""

from mailflow.adapters.servicebus.live import _MessageAdapter, _ReceiverAdapter


class _FakeRawMessage:
    """Stands in for azure.servicebus.ServiceBusReceivedMessage. Real received
    messages expose `.body` as bytes or as an iterable of bytes chunks (uAMQP data
    sections) -- NOT necessarily a JSON string from __str__, whose decoding behavior
    has varied across azure-servicebus versions. body_as_str() is implemented against
    `.body`, so the fake matches that shape."""

    def __init__(self, body_chunks: list[bytes]) -> None:
        self.body = body_chunks


class _FakeAzureReceiver:
    def __init__(self) -> None:
        self.completed: list[object] = []
        self.abandoned: list[object] = []

    def complete_message(self, message: object) -> None:
        self.completed.append(message)

    def abandon_message(self, message: object) -> None:
        self.abandoned.append(message)


def test_message_adapter_body_as_str_decodes_raw_body_chunks():
    raw = _FakeRawMessage([b'{"hello": ', b'"world"}'])
    adapter = _MessageAdapter(raw)

    assert adapter.body_as_str() == '{"hello": "world"}'
    assert adapter.raw is raw


def test_receiver_adapter_complete_passes_raw_message_not_the_adapter():
    raw = _FakeRawMessage([b"{}"])
    fake_receiver = _FakeAzureReceiver()
    adapter = _MessageAdapter(raw)

    _ReceiverAdapter(fake_receiver).complete(adapter)

    assert fake_receiver.completed == [raw]
    assert fake_receiver.abandoned == []


def test_receiver_adapter_abandon_passes_raw_message_not_the_adapter():
    raw = _FakeRawMessage([b"{}"])
    fake_receiver = _FakeAzureReceiver()
    adapter = _MessageAdapter(raw)

    _ReceiverAdapter(fake_receiver).abandon(adapter)

    assert fake_receiver.abandoned == [raw]
    assert fake_receiver.completed == []
