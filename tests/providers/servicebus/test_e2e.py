"""End-to-end + edge-case tests for the Azure Service Bus ingress path.

Unlike the per-unit tests (which use a fake pipeline), these drive the WHOLE flow through
a REAL pipeline:

    Service Bus message (Event Grid CloudEvent JSON)
      -> parse_eventgrid_message -> GraphNotification
      -> GraphProvider.submit -> pipeline.run_once()
         -> GraphProvider.fetch -> GraphClient GET (FAKE http transport, canned JSON)
         -> GraphEnvelopeParser -> GraphExtractor -> FilterChain
         -> Emitter.emit(EmailEvent) -> dedupe.mark_done -> cursor.commit_if_ahead
      -> receiver.complete(message)

No Azure SDK and no network: the Graph HTTP transport and the Service Bus receiver/message
are fakes; the stores are the real in-memory ones. This exercises CD-1 ack semantics,
at-least-once dedupe, on_filtered, lifecycle handling, and the malformed/404/500 edges.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from mailflow.adapters.graph.config import GraphConfig
from mailflow.adapters.servicebus.composition import build_servicebus_graph_runtime
from mailflow.adapters.servicebus.emitter import ServiceBusEmitter
from mailflow.core.models import StreamRef
from mailflow.emit.memory import MemoryEmitter
from mailflow.filters.deterministic import FunctionFilter
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

TENANT = "acme"
MBX = "ops@acme.com"
STREAM = StreamRef(mailbox=MBX, folder="inbox")


# --------------------------------------------------------------------------- fakes


class _FakeToken:
    def get_token(self) -> str:
        return "tok"


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}
        self.content = b""

    def json(self) -> Any:
        return self._payload


class _FakeGraphTransport:
    """Routes Graph REST GETs to canned JSON, keyed by the message id in the URL.

    `messages`: message_id -> message JSON (returned by GET .../messages/{id}).
    `attachments`: message_id -> attachment metadata list.
    `errors`: message_id -> HTTP status to return instead (e.g. 404 deleted, 500 infra).
    """

    def __init__(
        self,
        messages: dict[str, dict[str, Any]],
        attachments: dict[str, list[dict[str, Any]]] | None = None,
        errors: dict[str, int] | None = None,
    ) -> None:
        self.messages = messages
        self.attachments = attachments or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, *, headers: dict[str, str], json: Any | None) -> _Resp:
        self.calls.append((method, url))
        if "/attachments" in url:
            mid = url.split("/messages/")[1].split("/attachments")[0]
            return _Resp(200, {"value": self.attachments.get(mid, [])})
        if "/messages/" in url:  # single message GET: .../messages/{id}?$select=...
            mid = url.split("/messages/")[1].split("?")[0]
            if mid in self.errors:
                return _Resp(self.errors[mid], {"error": {"message": "boom"}})
            if mid in self.messages:
                return _Resp(200, self.messages[mid])
            return _Resp(404, {"error": {"message": "not found"}})
        # internetMessageId $filter fallback query (.../messages?$filter=...): no match
        return _Resp(200, {"value": []})


class _FakeReceiver:
    def __init__(self) -> None:
        self.completed: list[Any] = []
        self.abandoned: list[Any] = []

    def complete(self, message: Any) -> None:
        self.completed.append(message)

    def abandon(self, message: Any) -> None:
        self.abandoned.append(message)


class _Msg:
    def __init__(self, body: str) -> None:
        self._body = body

    def body_as_str(self) -> str:
        return self._body


class _FakeSbSender:
    """Matches the SbSender Protocol used by ServiceBusEmitter."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, *, body: bytes, message_id: str, content_type: str, session_id: str | None) -> None:
        self.sent.append(
            {"body": body, "message_id": message_id, "content_type": content_type, "session_id": session_id}
        )


# --------------------------------------------------------------------------- builders


def _graph_message(
    msg_id: str = "MSG1",
    *,
    sender: str = "alice@partner.com",
    subject: str = "Invoice #42",
    body: str = "hello body",
    content_type: str = "text",
    has_attachments: bool = False,
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "internetMessageId": f"<{msg_id}@partner.com>",
        "from": {"emailAddress": {"name": "Alice", "address": sender}},
        "toRecipients": [{"emailAddress": {"address": MBX}}],
        "ccRecipients": [],
        "subject": subject,
        "body": {"contentType": content_type, "content": body},
        "bodyPreview": body[:50],
        "receivedDateTime": "2026-07-03T10:00:00Z",
        "sentDateTime": "2026-07-03T09:59:00Z",
        "isDraft": False,
        "hasAttachments": has_attachments,
        "parentFolderId": "inbox",
        "categories": [],
    }


def _cloudevent(
    msg_id: str = "MSG1",
    *,
    user: str = MBX,
    subscription_id: str = "sub-1",
    client_state: str = "mailflow",
    change_type: str = "updated",
    include_resource: bool = True,
) -> dict[str, Any]:
    """One Event Grid CloudEvent wrapping a Graph message change notification."""
    data: dict[str, Any] = {
        "subscriptionId": subscription_id,
        "clientState": client_state,
        "changeType": change_type,
        "tenantId": "t-1",
        "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": msg_id},
    }
    if include_resource:
        data["resource"] = f"Users/{user}/Messages/{msg_id}"
    return {
        "id": f"ce-{msg_id}",
        "type": "Microsoft.Graph.MessageUpdated",
        "subject": f"Users/{user}/Messages/{msg_id}",   # fallback path uses this
        "specversion": "1.0",
        "data": data,
    }


def _lifecycle_cloudevent(subscription_id: str = "sub-1", client_state: str = "mailflow") -> dict[str, Any]:
    return {
        "type": "Microsoft.Graph.SubscriptionReauthorizationRequired",
        "subject": f"Users/{MBX}/mailFolders('inbox')",
        "data": {
            "subscriptionId": subscription_id,
            "clientState": client_state,
            "lifecycleEvent": "reauthorizationRequired",
            "resource": f"Users/{MBX}/mailFolders('inbox')",
        },
    }


def _build(
    *,
    messages: dict[str, dict[str, Any]] | None = None,
    attachments: dict[str, list[dict[str, Any]]] | None = None,
    errors: dict[str, int] | None = None,
    filters: list[Any] | None = None,
    on_filtered: str = "tag",
    emitter: Any | None = None,
    lifecycle_handler: Any = None,
):
    """Build a real ServiceBusRuntime wired to a real Graph pipeline over fakes.
    Returns (runtime, emitter, cursor_store, dedupe_store, transport)."""
    if messages is None:
        messages = {"MSG1": _graph_message("MSG1")}
    emitter = emitter if emitter is not None else MemoryEmitter()
    cursor_store = InMemoryCursorStore()
    dedupe_store = InMemoryDedupeStore()
    transport = _FakeGraphTransport(messages, attachments=attachments, errors=errors)
    runtime = build_servicebus_graph_runtime(
        graph_cfg=GraphConfig(
            tenant_id="t", client_id="c", client_secret_ref="env://X", mailboxes=[MBX],
        ),
        tenant=TENANT,
        token_provider=_FakeToken(),
        transport=transport,
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=cursor_store,
        dedupe_store=dedupe_store,
        blob_store=InMemoryBlobStore(),
        filters=filters,
        on_filtered=on_filtered,  # type: ignore[arg-type]
    )
    if lifecycle_handler is not None:
        runtime.lifecycle_handler = lifecycle_handler
    return runtime, emitter, cursor_store, dedupe_store, transport


# =========================================================================== E2E happy path


def test_e2e_single_message_emits_one_clean_email_and_completes():
    runtime, emitter, cursor_store, _dedupe, transport = _build()
    receiver = _FakeReceiver()
    msg = _Msg(json.dumps(_cloudevent("MSG1")))

    runtime.process_messages([msg], receiver=receiver)

    # exactly one EmailEvent emitted, fully populated from the fetched Graph JSON
    assert len(emitter.events) == 1
    ev = emitter.events[0]
    assert ev.tenant == TENANT
    assert ev.ordering_key == MBX
    assert ev.idempotency_key == f"{TENANT}|{MBX}|MSG1"
    assert ev.email.subject == "Invoice #42"
    assert ev.email.from_.address == "alice@partner.com"
    assert ev.email.body_text == "hello body"
    assert ev.email.disposition == "emitted"
    # the SB message was completed AFTER a successful run (CD-1), never abandoned
    assert receiver.completed == [msg]
    assert receiver.abandoned == []
    # cursor advanced on the terminal disposition (§8.1)
    assert cursor_store.get(TENANT, STREAM) is not None
    # the pipeline actually fetched the message over the (fake) Graph transport
    assert any("/messages/MSG1" in url for _m, url in transport.calls)


def test_e2e_ingress_to_servicebus_egress_roundtrip():
    """Full loop: SB ingress -> pipeline -> ServiceBusEmitter egress. The egress sender
    receives the serialized EmailEvent JSON."""
    sender = _FakeSbSender()
    runtime, _emitter, _c, _d, _t = _build(emitter=ServiceBusEmitter(sender=sender))
    receiver = _FakeReceiver()

    runtime.process_messages([_Msg(json.dumps(_cloudevent("MSG1")))], receiver=receiver)

    assert len(sender.sent) == 1
    sent = sender.sent[0]
    assert sent["content_type"] == "application/json"
    assert sent["message_id"] == f"{TENANT}|{MBX}|MSG1"
    assert sent["session_id"] == MBX                     # ordering_key -> session_id
    payload = json.loads(sent["body"].decode("utf-8"))
    assert payload["email"]["subject"] == "Invoice #42"


# =========================================================================== at-least-once / dedupe


def test_e2e_duplicate_delivery_is_deduped_both_completed():
    """At-least-once: the SAME message delivered twice emits ONCE (dedupe), and BOTH
    Service Bus messages are completed (redelivery is idempotent, not a failure)."""
    runtime, emitter, _c, _d, _t = _build()
    receiver = _FakeReceiver()
    m1 = _Msg(json.dumps(_cloudevent("MSG1")))
    m2 = _Msg(json.dumps(_cloudevent("MSG1")))  # identical -> same idempotency_key

    runtime.process_messages([m1], receiver=receiver)
    runtime.process_messages([m2], receiver=receiver)

    assert len(emitter.events) == 1                       # emitted exactly once
    assert receiver.completed == [m1, m2]                 # both acked
    assert receiver.abandoned == []


def test_e2e_array_of_two_notifications_emits_two():
    runtime, emitter, _c, _d, _t = _build(
        messages={"A1": _graph_message("A1", subject="one"), "A2": _graph_message("A2", subject="two")}
    )
    receiver = _FakeReceiver()
    body = json.dumps([_cloudevent("A1"), _cloudevent("A2")])   # one SB message, two events

    runtime.process_messages([_Msg(body)], receiver=receiver)

    assert sorted(e.email.subject for e in emitter.events) == ["one", "two"]
    assert len(receiver.completed) == 1


# =========================================================================== filtering


def test_e2e_on_filtered_drop_suppresses_but_still_completes():
    drop_partner = FunctionFilter(lambda env: "partner.com" not in env.from_.address)  # False -> drop
    runtime, emitter, cursor_store, _d, _t = _build(filters=[drop_partner], on_filtered="drop")
    receiver = _FakeReceiver()

    runtime.process_messages([_Msg(json.dumps(_cloudevent("MSG1")))], receiver=receiver)

    assert emitter.events == []                           # dropped, nothing delivered
    assert len(receiver.completed) == 1                   # message still acked
    assert cursor_store.get(TENANT, STREAM) is not None   # cursor advanced past the drop


def test_e2e_on_filtered_tag_delivers_with_filtered_disposition():
    drop_partner = FunctionFilter(lambda env: "partner.com" not in env.from_.address)
    runtime, emitter, _c, _d, _t = _build(filters=[drop_partner], on_filtered="tag")  # default
    receiver = _FakeReceiver()

    runtime.process_messages([_Msg(json.dumps(_cloudevent("MSG1")))], receiver=receiver)

    assert len(emitter.events) == 1
    assert emitter.events[0].email.disposition == "filtered"
    assert emitter.events[0].email.filter_reason


# =========================================================================== lifecycle


def test_e2e_lifecycle_message_handled_no_emit_completed():
    calls: list[Any] = []

    class _LC:
        def handle(self, ev: Any) -> None:
            calls.append(ev)

    runtime, emitter, _c, _d, _t = _build(lifecycle_handler=_LC())
    receiver = _FakeReceiver()

    runtime.process_messages([_Msg(json.dumps(_lifecycle_cloudevent()))], receiver=receiver)

    assert len(calls) == 1
    assert calls[0].lifecycle_event == "reauthorizationRequired"
    assert emitter.events == []                           # pipeline not run
    assert len(receiver.completed) == 1


# =========================================================================== security / malformed edges


def test_e2e_wrong_client_state_dropped_and_completed():
    runtime, emitter, _c, _d, _t = _build()
    receiver = _FakeReceiver()
    body = json.dumps(_cloudevent("MSG1", client_state="forged"))

    runtime.process_messages([_Msg(body)], receiver=receiver)

    assert emitter.events == []                           # forged clientState -> no notes
    assert len(receiver.completed) == 1                   # completed (nothing to process)


def test_e2e_malformed_cloudevent_data_not_a_dict_does_not_crash():
    runtime, emitter, _c, _d, _t = _build()
    receiver = _FakeReceiver()

    runtime.process_messages(
        [_Msg(json.dumps({"data": "oops"})), _Msg(json.dumps({"data": [1, 2]}))],
        receiver=receiver,
    )

    assert emitter.events == []
    assert len(receiver.completed) == 2                   # both acked, no AttributeError
    assert receiver.abandoned == []


def test_e2e_non_json_body_does_not_crash():
    runtime, emitter, _c, _d, _t = _build()
    receiver = _FakeReceiver()

    runtime.process_messages([_Msg("<<< not json >>>")], receiver=receiver)

    assert emitter.events == []
    assert len(receiver.completed) == 1


def test_e2e_subject_fallback_when_data_resource_absent():
    """CloudEvent with no data.resource still resolves the message via `subject`."""
    runtime, emitter, _c, _d, _t = _build(messages={"MSG9": _graph_message("MSG9", subject="fb")})
    receiver = _FakeReceiver()
    body = json.dumps(_cloudevent("MSG9", include_resource=False))

    runtime.process_messages([_Msg(body)], receiver=receiver)

    assert len(emitter.events) == 1
    assert emitter.events[0].email.subject == "fb"


# =========================================================================== fetch edges


def test_e2e_deleted_message_404_is_graceful():
    """Graph GET 404 (message deleted before fetch): no emit, no crash, message completed."""
    runtime, emitter, _c, _d, _t = _build(messages={}, errors={"MSG1": 404})
    receiver = _FakeReceiver()

    runtime.process_messages([_Msg(json.dumps(_cloudevent("MSG1")))], receiver=receiver)

    assert emitter.events == []
    assert len(receiver.completed) == 1
    assert receiver.abandoned == []


def test_e2e_infra_error_500_abandons_and_reraises_for_redelivery():
    """Graph GET 500 (transient infra): run_once raises -> message abandoned + re-raised
    so Service Bus redelivers (CD-1). NOT completed."""
    runtime, emitter, _c, _d, _t = _build(messages={}, errors={"MSG1": 500})
    receiver = _FakeReceiver()
    msg = _Msg(json.dumps(_cloudevent("MSG1")))

    with pytest.raises(Exception):
        runtime.process_messages([msg], receiver=receiver)

    assert emitter.events == []
    assert receiver.abandoned == [msg]                    # redelivery
    assert receiver.completed == []


# =========================================================================== content edges


def test_e2e_html_only_body_yields_readable_body_text():
    html = "<html><body><p>Hello,</p><p>send a <b>quote</b>.</p></body></html>"
    runtime, emitter, _c, _d, _t = _build(
        messages={"H1": _graph_message("H1", body=html, content_type="html")}
    )
    receiver = _FakeReceiver()

    runtime.process_messages([_Msg(json.dumps(_cloudevent("H1")))], receiver=receiver)

    assert len(emitter.events) == 1
    ce = emitter.events[0].email
    assert "Hello" in ce.body_text and "quote" in ce.body_text
    assert ce.body_html == html


def test_e2e_attachment_metadata_is_extracted():
    msg = _graph_message("AT1", has_attachments=True)
    atts = {"AT1": [{"id": "a1", "name": "invoice.pdf", "contentType": "application/pdf",
                     "size": 1234, "isInline": False}]}
    runtime, emitter, _c, _d, transport = _build(messages={"AT1": msg}, attachments=atts)
    receiver = _FakeReceiver()

    runtime.process_messages([_Msg(json.dumps(_cloudevent("AT1")))], receiver=receiver)

    assert len(emitter.events) == 1
    attachments = emitter.events[0].email.attachments
    assert len(attachments) == 1
    assert attachments[0].filename == "invoice.pdf"
    assert attachments[0].size_bytes == 1234
    # provider fetched the attachment list over the (fake) Graph transport
    assert any("/attachments" in url for _m, url in transport.calls)


# =========================================================================== batch edge


def test_e2e_empty_batch_is_noop():
    runtime, emitter, _c, _d, _t = _build()
    receiver = _FakeReceiver()

    runtime.process_messages([], receiver=receiver)

    assert emitter.events == []
    assert receiver.completed == []
    assert receiver.abandoned == []
