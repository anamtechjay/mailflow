"""REL-8 — the Event Hubs runtime must NOT checkpoint an event whose pipeline run
left a message non-terminal (transiently failed, to be retried).

The bug: `GraphEventHubsRuntime.process_batch` discarded the `RunReport` and called
`checkpointer.update(event)` unconditionally after `run_once()`. When a message failed
transiently, `run_once()` returns normally (no exception) with the message left pending
(cursor not advanced), yet the event got checkpointed → Event Hubs never redelivers it →
the message is silently lost.

Fix: checkpoint only when the run fully terminalized every fetched message
(`RunReport.all_terminal()`). A pending message holds the checkpoint so the event
redelivers; dedupe makes the redelivery of any already-done siblings harmless.
"""

from __future__ import annotations

import json

import pytest

from mailflow.adapters.graph.runtime import GraphEventHubsRuntime
from mailflow.core.observability import RunReport

pytestmark = pytest.mark.reliability

CLIENT_STATE = "mailflow"


def _notification(msg_id: str = "MSG1") -> str:
    return json.dumps({
        "value": [{
            "subscriptionId": "sub-1",
            "changeType": "created",
            "clientState": CLIENT_STATE,
            "resource": f"Users/ops@acme.com/Messages/{msg_id}",
            "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": msg_id},
        }]
    })


class _Event:
    def __init__(self, body: str) -> None:
        self._body = body

    def body_as_str(self) -> str:
        return self._body


class _Ckpt:
    def __init__(self) -> None:
        self.updated: list[object] = []

    def update(self, event: object) -> None:
        self.updated.append(event)


class _StubProvider:
    """Captures submitted notes; the runtime only needs .submit here."""

    def __init__(self) -> None:
        self.submitted: list[object] = []

    def submit(self, note: object) -> None:
        self.submitted.append(note)


class _StubPipeline:
    def __init__(self, report: RunReport) -> None:
        self._report = report
        self.runs = 0

    def run_once(self) -> RunReport:
        self.runs += 1
        return self._report


def _runtime(report: RunReport) -> tuple[GraphEventHubsRuntime, _StubPipeline]:
    pipe = _StubPipeline(report)
    rt = GraphEventHubsRuntime(
        provider=_StubProvider(),  # type: ignore[arg-type]
        pipeline=pipe,
        checkpointer=_Ckpt(),
        client_state=CLIENT_STATE,
    )
    return rt, pipe


def test_pending_message_holds_checkpoint():
    # fetched=1 but nothing terminal → the message is pending (transient retry).
    rt, pipe = _runtime(RunReport(fetched=1))
    ckpt = _Ckpt()
    rt.process_batch([_Event(_notification())], checkpointer=ckpt)

    assert pipe.runs == 1                 # the run happened
    assert ckpt.updated == []             # but the event was NOT checkpointed → redelivers


def test_fully_terminal_run_checkpoints():
    rt, pipe = _runtime(RunReport(fetched=1, emitted=1))
    ckpt = _Ckpt()
    rt.process_batch([_Event(_notification())], checkpointer=ckpt)

    assert pipe.runs == 1
    assert len(ckpt.updated) == 1         # all messages terminal → safe to checkpoint


def test_dead_lettered_run_checkpoints():
    # A poison message that reached the DLQ IS terminal — checkpoint past it.
    rt, pipe = _runtime(RunReport(fetched=1, dead_lettered=1))
    ckpt = _Ckpt()
    rt.process_batch([_Event(_notification())], checkpointer=ckpt)
    assert len(ckpt.updated) == 1


# --------------------------------------------------------------------------- Service Bus parity


def _sb_message(msg_id: str = "MSG1") -> str:
    return json.dumps({
        "type": "Microsoft.Graph.MessageCreated",
        "subject": f"Users/ops@acme.com/Messages/{msg_id}",
        "data": {
            "subscriptionId": "sub-1",
            "changeType": "created",
            "clientState": CLIENT_STATE,
            "resource": f"Users/ops@acme.com/Messages/{msg_id}",
            "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": msg_id},
        },
    })


class _SbMsg:
    def __init__(self, body: str) -> None:
        self._body = body

    def body_as_str(self) -> str:
        return self._body


class _Receiver:
    def __init__(self) -> None:
        self.completed: list[object] = []
        self.abandoned: list[object] = []

    def complete(self, message: object) -> None:
        self.completed.append(message)

    def abandon(self, message: object) -> None:
        self.abandoned.append(message)


def _sb_runtime(report: RunReport):
    from mailflow.adapters.servicebus.runtime import ServiceBusRuntime

    return ServiceBusRuntime(
        provider=_StubProvider(),  # type: ignore[arg-type]
        pipeline=_StubPipeline(report),
        client_state=CLIENT_STATE,
    )


def test_sb_pending_message_abandons_not_completes():
    rt = _sb_runtime(RunReport(fetched=1))          # pending → must redeliver
    rcv = _Receiver()
    msg = _SbMsg(_sb_message())
    rt.process_messages([msg], receiver=rcv)
    assert rcv.completed == []
    assert rcv.abandoned == [msg]


def test_sb_terminal_message_completes():
    rt = _sb_runtime(RunReport(fetched=1, emitted=1))
    rcv = _Receiver()
    msg = _SbMsg(_sb_message())
    rt.process_messages([msg], receiver=rcv)
    assert rcv.completed == [msg]
    assert rcv.abandoned == []
