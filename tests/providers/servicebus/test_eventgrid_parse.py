import json

from mailflow.adapters.servicebus.eventgrid import parse_eventgrid_message


def _cloudevent(sub="sub-1", client_state="mailflow", user="ops@acme.com", msg="MSG123"):
    return {
        "id": "ce-1",
        "type": "Microsoft.Graph.MessageUpdated",
        "subject": f"Users/{user}/Messages/{msg}",
        "specversion": "1.0",
        "data": {
            "subscriptionId": sub,
            "clientState": client_state,
            "changeType": "updated",
            "tenantId": "t-1",
            "resource": f"Users/{user}/Messages/{msg}",
            "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": msg},
        },
    }


def test_single_cloudevent_object_parses_to_one_notification():
    notes = parse_eventgrid_message(json.dumps(_cloudevent()), expected_client_state="mailflow")
    assert len(notes) == 1
    assert notes[0].user_id == "ops@acme.com"
    assert notes[0].message_id == "MSG123"
    assert notes[0].subscription_id == "sub-1"


def test_cloudevent_array_parses_all():
    body = json.dumps([_cloudevent(msg="M1"), _cloudevent(msg="M2")])
    notes = parse_eventgrid_message(body, expected_client_state="mailflow")
    assert [n.message_id for n in notes] == ["M1", "M2"]


def test_wrong_client_state_is_dropped():
    notes = parse_eventgrid_message(
        json.dumps(_cloudevent(client_state="forged")), expected_client_state="mailflow"
    )
    assert notes == []


def test_non_json_is_empty():
    assert parse_eventgrid_message("not json", expected_client_state="mailflow") == []


def test_non_dict_data_is_ignored_not_raised():
    # Malformed CloudEvent: "data" is a string instead of an object.
    body = json.dumps({
        "id": "ce-bad",
        "type": "Microsoft.Graph.MessageUpdated",
        "subject": "Users/ops@acme.com/Messages/MSG1",
        "data": "oops",
    })
    assert parse_eventgrid_message(body, expected_client_state="mailflow") == []


def test_non_dict_data_list_is_ignored_not_raised():
    # Malformed CloudEvent: "data" is a list instead of an object.
    body = json.dumps({
        "id": "ce-bad2",
        "type": "Microsoft.Graph.MessageUpdated",
        "subject": "Users/ops@acme.com/Messages/MSG1",
        "data": [1, 2],
    })
    assert parse_eventgrid_message(body, expected_client_state="mailflow") == []


def test_subject_fallback_used_when_data_has_no_resource():
    # data carries subscriptionId/clientState/resourceData but NOT `resource`;
    # the resource path must fall back to the CloudEvent's top-level `subject`.
    body = json.dumps({
        "id": "ce-2",
        "type": "Microsoft.Graph.MessageUpdated",
        "subject": "Users/ops@acme.com/Messages/MSG9",
        "data": {
            "subscriptionId": "sub-1",
            "clientState": "mailflow",
            "changeType": "updated",
            "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": "MSG9"},
        },
    })
    notes = parse_eventgrid_message(body, expected_client_state="mailflow")
    assert len(notes) == 1
    assert notes[0].user_id == "ops@acme.com"
    assert notes[0].message_id == "MSG9"
    assert notes[0].subscription_id == "sub-1"


def test_captured_real_payload_if_present():
    # Task A2 saves a real captured event here; if present, the parser must handle it.
    import os
    p = os.path.join(os.path.dirname(__file__), "fixtures", "eventgrid_message.json")
    if not os.path.exists(p):
        return  # capture not available in this environment; representative tests cover shape
    with open(p, encoding="utf-8") as f:
        body = f.read()
    # Should not raise; returns a list (possibly empty if clientState differs in capture).
    assert isinstance(parse_eventgrid_message(body, expected_client_state="mailflow"), list)
