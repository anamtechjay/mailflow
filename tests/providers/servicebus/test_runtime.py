import json

from mailflow.adapters.servicebus.runtime import ServiceBusRuntime


class _FakeProvider:
    PROVIDER = "graph"
    def __init__(self): self.submitted = []
    def submit(self, note): self.submitted.append(note)


class _FakePipeline:
    def __init__(self, raises=False): self.runs = 0; self._raises = raises
    def run_once(self):
        self.runs += 1
        if self._raises:
            raise RuntimeError("infra down")
        return object()


class _FakeReceiver:
    def __init__(self): self.completed = []; self.abandoned = []
    def complete(self, m): self.completed.append(m)
    def abandon(self, m): self.abandoned.append(m)


class _Msg:
    def __init__(self, body): self._b = body
    def body_as_str(self): return self._b


def _cloudevent_body(msg="MSG1"):
    return json.dumps({
        "type": "Microsoft.Graph.MessageUpdated",
        "subject": f"Users/ops@acme.com/Messages/{msg}",
        "data": {
            "subscriptionId": "sub-1", "clientState": "mailflow", "changeType": "updated",
            "resource": f"Users/ops@acme.com/Messages/{msg}",
            "resourceData": {"id": msg},
        },
    })


def test_notification_message_submits_runs_and_completes():
    provider, pipeline, receiver = _FakeProvider(), _FakePipeline(), _FakeReceiver()
    rt = ServiceBusRuntime(provider=provider, pipeline=pipeline, client_state="mailflow")
    m = _Msg(_cloudevent_body())
    rt.process_messages([m], receiver=receiver)

    assert len(provider.submitted) == 1
    assert pipeline.runs == 1
    assert receiver.completed == [m]      # completed AFTER a successful run (CD-1)
    assert receiver.abandoned == []


def test_infra_failure_abandons_and_reraises_for_redelivery():
    import pytest
    provider, pipeline, receiver = _FakeProvider(), _FakePipeline(raises=True), _FakeReceiver()
    rt = ServiceBusRuntime(provider=provider, pipeline=pipeline, client_state="mailflow")
    m = _Msg(_cloudevent_body())
    with pytest.raises(RuntimeError):
        rt.process_messages([m], receiver=receiver)
    assert receiver.abandoned == [m]      # NOT completed -> Service Bus redelivers
    assert receiver.completed == []


def test_lifecycle_only_message_is_completed_without_pipeline_run():
    provider, pipeline, receiver = _FakeProvider(), _FakePipeline(), _FakeReceiver()
    calls = []

    class _LC:
        def handle(self, ev): calls.append(ev)

    rt = ServiceBusRuntime(
        provider=provider, pipeline=pipeline, client_state="mailflow", lifecycle_handler=_LC()
    )
    body = json.dumps({
        "type": "Microsoft.Graph.SubscriptionReauthorizationRequired",
        "data": {"subscriptionId": "sub-1", "clientState": "mailflow",
                 "lifecycleEvent": "reauthorizationRequired",
                 "resource": "Users/ops@acme.com/mailFolders('inbox')"},
    })
    rt.process_messages([_Msg(body)], receiver=receiver)
    assert len(calls) == 1
    assert pipeline.runs == 0
    assert len(receiver.completed) == 1
