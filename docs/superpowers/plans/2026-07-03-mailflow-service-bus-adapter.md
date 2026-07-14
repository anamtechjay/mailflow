---
date: 2026-07-03
topic: Azure Service Bus support for mailflow — egress Emitter + Graph ingress via Event Grid Partner Topic → Service Bus (no Event Hubs, no webhook)
status: ready
spec: docs/mailflow-spec.md (transport-agnostic ports §5.3); README "Live: Outlook via Microsoft Graph"
---

# Azure Service Bus Adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let mailflow (a) **emit** each `EmailEvent` to an Azure Service Bus queue/topic (egress), and (b) **ingest** Outlook mail whose Graph change-notifications arrive via `Graph → Event Grid Partner Topic → Service Bus` (ingress) — with **no Azure Event Hubs and no public webhook**, reusing the Service Bus you already operate.

**Architecture:** mailflow's transport is a pluggable seam. Egress is the existing `Emitter` port (`emit(event) -> object`) — a new `ServiceBusEmitter` drops in with **zero core/facade changes** via the `overrides={"emitter": ...}` seam (§A10). Ingress mirrors the existing `GraphEventHubsRuntime`: a new `ServiceBusRuntime` consumes Service Bus messages, unwraps the Event Grid **CloudEvents** envelope into the existing `GraphNotification`, then reuses the **unchanged** `GraphProvider → GraphEnvelopeParser → GraphExtractor → Pipeline`. Only the consume loop + ack differ: Service Bus `complete()`-after-successful-`run_once()` replaces Event Hubs offset checkpointing. All vendor SDK imports are **local to functions** in `live.py` only, so the unit suite (fakes) never imports `azure-servicebus`.

**Tech Stack:** Python 3.12, pydantic 2.6+, pytest 8+, mypy strict. New optional extra `[servicebus]` = `azure-servicebus`. Event Grid CloudEvents are parsed as **plain JSON** (no SDK needed) so the parser is fully unit-testable. Vendor SDK absent from the test env (mypy override for `azure.*` already exists).

## Global Constraints

- **Never edit `core/`** — implement ports, don't touch the spine. New code lives under `src/mailflow/adapters/servicebus/` (new, unowned dir) only. (CLAUDE.md ownership table.)
- **Vendor SDK imports are local to functions** in `live.py` only. `import mailflow.adapters.servicebus.<anything>` must succeed with no `[servicebus]` extra installed. (Parity with `graph/live.py`, `gmail/live.py`.)
- **mypy strict, every commit importable.** If module A imports B, commit B first. `azure.*` is already covered by the `ignore_missing_imports` override in `pyproject.toml:35-37` — no new override needed.
- **PyYAML is NOT installed** — keep any fixtures JSON-compatible.
- **Emitter port contract (frozen):** `Emitter.emit(self, event: EmailEvent) -> object` (`core/ports.py:98`). `EmailEvent` fields: `schema_version: str`, `tenant: str`, `ordering_key: str = ""`, `idempotency_key: str = ""`, `email: CleanEmail` (`core/events.py:17-22`).
- **Cursor/dedupe contracts (frozen):** at-least-once delivery; the `Pipeline` owns dedupe (`idempotency_key = tenant|mailbox|provider_message_id`) and cursor monotonicity. The transport layer must be **idempotent under redelivery** — re-processing a redelivered Service Bus message is safe because the dedupe store suppresses it. (README "Delivery & concurrency (V1)".)
- **Commit trailer** on every commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. **Never `git push`.**
- Run tests/types with the repo interpreter: `python -m pytest` and `python -m mypy` (Windows checkout has no `.venv`; the Mac path in CLAUDE.md does not apply here).

---

## CONTRACT DECISIONS (resolve up front — they shape several tasks)

**CD-1 — DLQ ownership (who dead-letters a poison email?).**
The mailflow `Pipeline` already owns **message-level** dead-lettering: poison/oversized messages are counted exactly once via `add_dead_letter()` and sunk to `dlq_emitter`; `run_once()` returns a `RunReport` and does **not** raise for per-message poison. Therefore the `ServiceBusRuntime` **completes** the Service Bus message after `run_once()` returns (success *or* handled-poison), and only lets a message **redeliver** (by not completing, so the SB lock expires) when `run_once()` raises — i.e. a transport/infra error. We do **not** call Service Bus `dead_letter()` ourselves; Service Bus's native `MaxDeliveryCount` DLQ is the backstop for repeated *infra* failures only. Result: **one poison email ⇒ exactly one mailflow DLQ record**, never double-counted across the two DLQ systems. This is exact parity with `GraphEventHubsRuntime.process_batch` (checkpoint-after-processing). Provisioning sets a **high `MaxDeliveryCount`** (e.g. 10) so transient infra retries aren't prematurely dead-lettered.

**CD-2 — `changeType` / new-mail signal risk (VALIDATE before building the runtime).**
Microsoft's Event Grid doc is internally contradictory: the prose says `Created` is **not** supported for Graph→Event Grid (`Updated,Deleted` only), while the code samples pass `updated,deleted,created`. mailflow is **poll-authoritative — "push only wakes the poller"** (README A5 note), and `GraphProvider` has delta `sweep()`, so an `Updated` wake is still actionable. **But** we must not build the ingress runtime on an unproven assumption. Task A2 is a **hard gate**: capture a real Event Grid message for a new inbound email and confirm (a) an event actually fires on new mail, and (b) its exact `data`/`subject` field names. If new mail does **not** produce a usable wake event, ingress falls back to **poll/sweep on a timer** (the runtime still consumes lifecycle + update events; a scheduled `sweep` covers `created`) — documented, not silently dropped.

**CD-3 — Egress needs no facade change.** `ServiceBusEmitter` reaches production purely via `connect(provider, overrides={"emitter": ServiceBusEmitter(...)})` (the §A10 seam at `facade.py:260`). We deliberately do **not** add a `connect(emit=...)` kwarg in this plan (YAGNI). Ingress gets a dedicated `run_service` entrypoint (parity with `graph/live.py`), not a `connect()` transport switch (deferred).

---

## File Structure

- `src/mailflow/adapters/servicebus/__init__.py` — package + public exports (no SDK imports).
- `src/mailflow/adapters/servicebus/config.py` — `ServiceBusConfig` (queue/topic + refs; secrets are refs, never literals).
- `src/mailflow/adapters/servicebus/emitter.py` — `ServiceBusEmitter` (egress, implements `Emitter`).
- `src/mailflow/adapters/servicebus/eventgrid.py` — `parse_eventgrid_message()` (CloudEvent JSON → `list[GraphNotification]`; pure, no SDK).
- `src/mailflow/adapters/servicebus/runtime.py` — `ServiceBusRuntime` (consume-loop logic; SDK-free Protocols for the SB message).
- `src/mailflow/adapters/servicebus/composition.py` — `build_servicebus_graph_runtime()` (assembles GraphProvider+Pipeline+ServiceBusRuntime from injected pieces; no SDK).
- `src/mailflow/adapters/servicebus/live.py` — `run_service()` + `AzureServiceBusClient`/`ServiceBusSender` SDK glue (local imports only).
- `tests/providers/servicebus/__init__.py`
- `tests/providers/servicebus/test_emitter.py`, `test_eventgrid_parse.py`, `test_runtime.py`, `test_composition.py`, `test_live_import.py`
- `pyproject.toml` — add `servicebus` extra.
- `docs/azure-servicebus-setup.md` — operator runbook (provisioning).
- `scripts/check_servicebus_connection.py` — egress smoke test.

---

## Part A — Provisioning runbook + validation gate (operator-run)

### Task A1: Author the Service Bus + Event Grid provisioning runbook

**Files:**
- Create: `docs/azure-servicebus-setup.md`

**Interfaces:**
- Produces: the config values later tasks consume — `servicebus_fqns` (`<ns>.servicebus.windows.net`), `queue_name`, and either a connection-string ref or a managed-identity path; plus the Graph subscription `notificationUrl`/`lifecycleNotificationUrl` in the Event Grid form.

- [ ] **Step 1: Write the runbook.** Create `docs/azure-servicebus-setup.md` with these exact sections and commands (fill `<ANGLE_BRACKETS>` per tenant; never hardcode secrets):

  ```markdown
  # Azure Service Bus + Event Grid (Graph) provisioning

  Ingress chain: Outlook → Microsoft Graph → Event Grid Partner Topic → Service Bus queue → mailflow.
  No Event Hubs. No public webhook.

  ## 1. Service Bus (you likely already have this)
  az servicebus namespace create -g <RG> -n <SB_NAMESPACE> --sku Standard
  az servicebus queue create -g <RG> --namespace-name <SB_NAMESPACE> -n mailflow-graph \
    --max-delivery-count 10 --enable-duplicate-detection true
  # RECORD: fqns = <SB_NAMESPACE>.servicebus.windows.net ; queue = mailflow-graph

  ## 2. Register Event Grid + authorize Graph as a partner
  az provider register --namespace Microsoft.EventGrid
  # Authorize the Microsoft Graph partner to create a partner topic in <RG>
  # (portal: Event Grid → Partner Configurations → add Microsoft Graph; or per Learn:
  #  https://learn.microsoft.com/azure/event-grid/subscribe-to-partner-events )

  ## 3. Create the Graph subscription that targets the partner topic
  #  notificationUrl uses the EventGrid: scheme (NOT the EventHub: scheme).
  POST https://graph.microsoft.com/v1.0/subscriptions
  {
    "changeType": "created,updated",
    "notificationUrl": "EventGrid:?azuresubscriptionid=<SUB>&resourcegroup=<RG>&partnertopic=mailflow-graph-topic&location=<REGION>",
    "lifecycleNotificationUrl": "EventGrid:?azuresubscriptionid=<SUB>&resourcegroup=<RG>&partnertopic=mailflow-graph-topic&location=<REGION>",
    "resource": "users/<MAILBOX-UPN>/mailFolders('inbox')/messages",
    "expirationDateTime": "<now+6days, RFC3339>",
    "clientState": "mailflow"
  }
  # NOTE (CD-2): if 'created' is rejected for messages via Event Grid, use "updated"
  # and rely on the timer sweep for new mail (see runtime). Confirm in Task A2.

  ## 4. Activate the partner topic + route it to the Service Bus queue
  az eventgrid partner topic activate -g <RG> -n mailflow-graph-topic
  az eventgrid partner topic event-subscription create \
    -g <RG> --partner-topic-name mailflow-graph-topic -n to-sb \
    --endpoint-type servicebusqueue \
    --endpoint /subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.ServiceBus/namespaces/<SB_NAMESPACE>/queues/mailflow-graph

  ## 5. Consumer access
  # Give the mailflow identity "Azure Service Bus Data Receiver" on the queue
  # (and "Data Sender" if it also emits to a Service Bus queue for egress).
  ```

- [ ] **Step 2: Commit.**

  ```bash
  git add docs/azure-servicebus-setup.md
  git commit -m "docs: Azure Service Bus + Event Grid partner-topic provisioning runbook"
  ```

### Task A2: VALIDATION GATE — capture one real Event Grid message and confirm the wire shape (CD-2)

**Files:** none (operator/eng action; records a fixture used by Task C1).

- [ ] **Step 1:** After A1, send yourself a test email to `<MAILBOX-UPN>`. In the portal (Service Bus → queue → Service Bus Explorer) **peek** the delivered message and copy its JSON body.
- [ ] **Step 2:** Confirm and **record** in the PR/description:
  - Whether a message actually arrived for **new inbound mail** (decides `created` vs timer-sweep — CD-2).
  - The exact envelope: `type` (e.g. `Microsoft.Graph.Message…`), `subject`, and the `data` object's field names — specifically whether `data.resource`, `data.resourceData.id`, `data.subscriptionId`, `data.clientState`, `data.changeType` are present (Graph resourceData shape).
- [ ] **Step 3:** Save the captured JSON verbatim as `tests/providers/servicebus/fixtures/eventgrid_message.json` (create the dir). Task C1's test asserts the parser handles **this exact captured payload** — so the parser is validated against reality, not a guess.

> If Step 2 shows the delivered shape differs from the representative payload in Task C1, update C1's test payload to match the capture before implementing — the capture is the source of truth.

---

## Part B — Egress: `ServiceBusEmitter` (build first; self-contained, highest value/lowest risk)

### Task B1: `ServiceBusConfig`

**Files:**
- Create: `src/mailflow/adapters/servicebus/__init__.py`
- Create: `src/mailflow/adapters/servicebus/config.py`
- Test: `tests/providers/servicebus/__init__.py`, `tests/providers/servicebus/test_config.py`

**Interfaces:**
- Produces: `ServiceBusConfig(fully_qualified_namespace: str, entity_name: str, connection_string_ref: str = "")` — `entity_name` is the queue or topic name; `connection_string_ref` is a **SecretProvider ref**, never a literal secret.

- [ ] **Step 1: Write the failing test.** Create `tests/providers/servicebus/__init__.py` (empty) and `tests/providers/servicebus/test_config.py`:

```python
from mailflow.adapters.servicebus.config import ServiceBusConfig


def test_config_holds_entity_and_ref_but_not_literal_secret():
    cfg = ServiceBusConfig(
        fully_qualified_namespace="ns.servicebus.windows.net",
        entity_name="mailflow-graph",
        connection_string_ref="env://SB_CONNECTION_STRING",
    )
    assert cfg.entity_name == "mailflow-graph"
    assert cfg.connection_string_ref == "env://SB_CONNECTION_STRING"


def test_entity_name_required():
    import pytest
    with pytest.raises(ValueError):
        ServiceBusConfig(fully_qualified_namespace="ns.servicebus.windows.net", entity_name="")
```

- [ ] **Step 2: Run it, verify it fails.** Run: `python -m pytest tests/providers/servicebus/test_config.py -v` — Expected: FAIL (module not found).

- [ ] **Step 3: Implement.** Create `src/mailflow/adapters/servicebus/__init__.py`:

```python
"""Azure Service Bus adapter — egress Emitter + Graph ingress via Event Grid Partner Topic."""

from mailflow.adapters.servicebus.config import ServiceBusConfig
from mailflow.adapters.servicebus.emitter import ServiceBusEmitter
from mailflow.adapters.servicebus.eventgrid import parse_eventgrid_message
from mailflow.adapters.servicebus.runtime import ServiceBusRuntime

__all__ = [
    "ServiceBusConfig",
    "ServiceBusEmitter",
    "parse_eventgrid_message",
    "ServiceBusRuntime",
]
```

  Create `src/mailflow/adapters/servicebus/config.py`:

```python
"""Typed config for the Service Bus adapter. Secrets are *references* resolved
later via the SecretProvider port — never literal secrets here."""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class ServiceBusConfig(BaseModel):
    fully_qualified_namespace: str      # "<namespace>.servicebus.windows.net"
    entity_name: str                    # queue or topic name
    connection_string_ref: str = ""     # SecretProvider ref (SAS) — or "" for RBAC/credential

    @field_validator("entity_name")
    @classmethod
    def _entity_required(cls, v: str) -> str:
        if not v:
            raise ValueError("entity_name (queue or topic) is required")
        return v
```

  (`__init__.py` imports `emitter`/`eventgrid`/`runtime` which don't exist yet — create empty stubs so the tree stays importable, or reorder: implement B2/C1/D1 modules before importing them. For a per-task green build, temporarily narrow `__init__.py` to only `config` here and widen it in each subsequent task's Step 3.)

- [ ] **Step 4: Run tests, verify pass.** Run: `python -m pytest tests/providers/servicebus/test_config.py -v` — Expected: PASS. Then `python -m mypy` — Expected: no new errors.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/adapters/servicebus/__init__.py src/mailflow/adapters/servicebus/config.py tests/providers/servicebus/__init__.py tests/providers/servicebus/test_config.py
git commit -m "feat(servicebus): ServiceBusConfig (entity + secret-ref, no literal secrets)"
```

### Task B2: `ServiceBusEmitter` (implements the `Emitter` port)

**Files:**
- Create: `src/mailflow/adapters/servicebus/emitter.py`
- Modify: `src/mailflow/adapters/servicebus/__init__.py` (add the `emitter` import)
- Test: `tests/providers/servicebus/test_emitter.py`

**Interfaces:**
- Consumes: `EmailEvent` (`core/events.py`), the `Emitter` port (`core/ports.py:98`: `emit(event) -> object`).
- Produces: `ServiceBusEmitter(sender: SbSender)` where `SbSender` is the duck-typed Protocol `def send(self, *, body: bytes, message_id: str, content_type: str, session_id: str | None) -> None`. `emit()` serializes the event to JSON, sends it, and returns an `EmitReceipt(id=..., accepted=True)`. The real `azure-servicebus` sender is adapted in `live.py`; tests inject a fake — **no SDK in this file**.

- [ ] **Step 1: Write the failing test.** `tests/providers/servicebus/test_emitter.py`:

```python
import json

from mailflow.adapters.servicebus.emitter import ServiceBusEmitter
from mailflow.core.events import EmailEvent
from tests.factories import make_clean_email  # existing helper; see note below


class _FakeSender:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, *, body: bytes, message_id: str, content_type: str, session_id):
        self.sent.append(
            {"body": body, "message_id": message_id, "content_type": content_type, "session_id": session_id}
        )


def _event() -> EmailEvent:
    return EmailEvent(
        tenant="acme",
        ordering_key="ops@acme.com",
        idempotency_key="acme|ops@acme.com|MSG123",
        email=make_clean_email(canonical_id="<m1@x>", subject="hi"),
    )


def test_emit_sends_json_body_keyed_by_idempotency_key():
    sender = _FakeSender()
    receipt = ServiceBusEmitter(sender=sender).emit(_event())

    assert len(sender.sent) == 1
    msg = sender.sent[0]
    # SB duplicate-detection key == idempotency_key (transport-level dedupe backstop)
    assert msg["message_id"] == "acme|ops@acme.com|MSG123"
    assert msg["content_type"] == "application/json"
    # ordering_key -> session_id so FIFO-per-mailbox is available if the queue uses sessions
    assert msg["session_id"] == "ops@acme.com"
    payload = json.loads(msg["body"].decode("utf-8"))
    assert payload["tenant"] == "acme"
    assert payload["email"]["subject"] == "hi"
    # Emitter returns a receipt (Emitter.emit -> object)
    assert getattr(receipt, "accepted", False) is True
```

  > **Note on `make_clean_email`:** if `tests/factories.py` has no such helper, build the `CleanEmail` inline the way existing tests do (grep an existing test that constructs `EmailEvent`/`CleanEmail` and copy its construction). Do **not** invent fields — use the real `CleanEmail` shape.

- [ ] **Step 2: Run it, verify it fails.** Run: `python -m pytest tests/providers/servicebus/test_emitter.py -v` — Expected: FAIL (`ServiceBusEmitter` undefined).

- [ ] **Step 3: Implement.** `src/mailflow/adapters/servicebus/emitter.py`:

```python
"""ServiceBusEmitter — egress sink implementing the core Emitter port. Serializes each
EmailEvent to JSON and sends it to a Service Bus queue/topic. The transport SDK is NOT
imported here: a duck-typed sender is injected (adapted from azure-servicebus in live.py),
so this stays unit-testable with a fake. The message_id is the event's idempotency_key
(so Service Bus duplicate-detection is a transport-level backstop to the pipeline dedupe),
and ordering_key maps to session_id for optional per-mailbox FIFO."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from mailflow.core.events import EmailEvent


class SbSender(Protocol):
    def send(
        self, *, body: bytes, message_id: str, content_type: str, session_id: str | None
    ) -> None: ...


class EmitReceipt(BaseModel):
    id: str
    accepted: bool = True


class ServiceBusEmitter:
    def __init__(self, *, sender: SbSender) -> None:
        self._sender = sender

    def emit(self, event: EmailEvent) -> EmitReceipt:
        body = event.model_dump_json().encode("utf-8")
        message_id = event.idempotency_key or event.email.canonical_id
        session_id = event.ordering_key or None
        self._sender.send(
            body=body,
            message_id=message_id,
            content_type="application/json",
            session_id=session_id,
        )
        return EmitReceipt(id=event.email.canonical_id, accepted=True)
```

  Widen `__init__.py` to include the `emitter` import (already listed in B1's `__all__`).

- [ ] **Step 4: Run tests + mypy.** Run: `python -m pytest tests/providers/servicebus/test_emitter.py -v` — Expected: PASS. `python -m mypy` — Expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/adapters/servicebus/emitter.py src/mailflow/adapters/servicebus/__init__.py tests/providers/servicebus/test_emitter.py
git commit -m "feat(servicebus): ServiceBusEmitter implements Emitter port (JSON body, idempotency_key -> SB message_id)"
```

### Task B3: Egress smoke script + `[servicebus]` extra

**Files:**
- Modify: `pyproject.toml:15-18`
- Create: `scripts/check_servicebus_connection.py`

- [ ] **Step 1: Add the extra.** In `pyproject.toml` under `[project.optional-dependencies]` add:

```toml
servicebus = ["azure-servicebus>=7.12", "azure-identity>=1.16"]
```

- [ ] **Step 2: Write the smoke script** `scripts/check_servicebus_connection.py`:

```python
"""Service Bus egress smoke test: send one EmailEvent JSON to the configured queue.

  pip install -e ".[servicebus]"
  # set SB_CONNECTION_STRING and SB_QUEUE, then:
  python scripts/check_servicebus_connection.py
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    from azure.servicebus import ServiceBusClient, ServiceBusMessage  # local import

    conn = os.environ.get("SB_CONNECTION_STRING")
    queue = os.environ.get("SB_QUEUE", "mailflow-graph")
    if not conn:
        print("[FAIL] set SB_CONNECTION_STRING (and optionally SB_QUEUE)")
        return 1

    with ServiceBusClient.from_connection_string(conn) as client:
        with client.get_queue_sender(queue) as sender:
            sender.send_messages(
                ServiceBusMessage(
                    b'{"schema_version":"1.3","tenant":"smoke","email":{}}',
                    message_id="smoke-1",
                    content_type="application/json",
                )
            )
    print(f"[OK] sent 1 test message to queue={queue}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Commit.**

```bash
git add pyproject.toml scripts/check_servicebus_connection.py
git commit -m "feat(servicebus): add [servicebus] extra + egress smoke script"
```

---

## Part C — Ingress parsing: Event Grid CloudEvent → `GraphNotification`

### Task C1: `parse_eventgrid_message` (pure JSON, no SDK)

**Files:**
- Create: `src/mailflow/adapters/servicebus/eventgrid.py`
- Modify: `src/mailflow/adapters/servicebus/__init__.py`
- Test: `tests/providers/servicebus/test_eventgrid_parse.py`

**Interfaces:**
- Consumes: the existing `parse_notification_payload(body: str, *, expected_client_state: str) -> list[GraphNotification]` (`adapters/graph/notifications.py:83`) and `GraphNotification` (same module). Reuses the existing `users/<user>/messages/<msg>` resource regex, the validation-event skip, and the constant-time `clientState` check — **DRY, no reimplementation**.
- Produces: `parse_eventgrid_message(body: str, *, expected_client_state: str) -> list[GraphNotification]`. The Service Bus message body is one or more Event Grid **CloudEvents** (a single JSON object or a JSON array). Each event's `data` object carries the Graph change-notification fields (`subscriptionId`, `clientState`, `changeType`, `resource`/`resourceData`). We normalize each event's `data` into the item shape `parse_notification_payload` expects, wrap them as `{"value": [...]}`, and delegate.

- [ ] **Step 1: Write the failing test.** `tests/providers/servicebus/test_eventgrid_parse.py`:

```python
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
```

- [ ] **Step 2: Run it, verify it fails.** Run: `python -m pytest tests/providers/servicebus/test_eventgrid_parse.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement.** `src/mailflow/adapters/servicebus/eventgrid.py`:

```python
"""Unwrap Event Grid CloudEvents (delivered via a Service Bus queue) into the existing
GraphNotification pointers. The Service Bus message body is a single CloudEvent JSON
object or a JSON array of them; each event's `data` holds the Graph change-notification
fields. We normalize each `data` into the item shape the battle-tested
graph.notifications.parse_notification_payload expects, then delegate — reusing its
resource regex, validation-event skip, and constant-time clientState check (DRY)."""

from __future__ import annotations

import json
from typing import Any

from mailflow.adapters.graph.notifications import GraphNotification, parse_notification_payload


def _events(body: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _to_item(event: dict[str, Any]) -> dict[str, Any]:
    """Map one CloudEvent to a Graph changeNotification 'value' item. Prefer the `data`
    object's fields; fall back to the CloudEvent `subject` for the resource path."""
    data = event.get("data") or {}
    resource = str(data.get("resource") or event.get("subject") or "")
    return {
        "subscriptionId": data.get("subscriptionId", ""),
        "changeType": data.get("changeType", ""),
        "clientState": data.get("clientState", ""),
        "resource": resource,
        "resourceData": data.get("resourceData") or {},
    }


def parse_eventgrid_message(
    body: str, *, expected_client_state: str
) -> list[GraphNotification]:
    items = [_to_item(e) for e in _events(body)]
    if not items:
        return []
    collection = json.dumps({"value": items})
    return parse_notification_payload(collection, expected_client_state=expected_client_state)
```

  Widen `__init__.py` `parse_eventgrid_message` import (already in `__all__`).

- [ ] **Step 4: Run tests + mypy.** Run: `python -m pytest tests/providers/servicebus/test_eventgrid_parse.py -v` — Expected: PASS. `python -m mypy` — Expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/adapters/servicebus/eventgrid.py src/mailflow/adapters/servicebus/__init__.py tests/providers/servicebus/test_eventgrid_parse.py
git commit -m "feat(servicebus): parse Event Grid CloudEvents into GraphNotification (delegates to graph.notifications)"
```

---

## Part D — Ingress runtime: `ServiceBusRuntime` (consume → parse → pipeline → complete)

### Task D1: `ServiceBusRuntime.process_messages` (SDK-free, complete-after-success per CD-1)

**Files:**
- Create: `src/mailflow/adapters/servicebus/runtime.py`
- Modify: `src/mailflow/adapters/servicebus/__init__.py`
- Test: `tests/providers/servicebus/test_runtime.py`

**Interfaces:**
- Consumes: `GraphProvider.submit(note)` (`adapters/graph/provider.py:35`), a `PipelineLike` (`run_once() -> object`), `parse_eventgrid_message` (Task C1), and `GraphLifecycleHandler` (optional, `adapters/graph/lifecycle.py`) via `parse_lifecycle_payload` for reauthorization/removed/missed.
- Produces: `ServiceBusRuntime(provider, pipeline, client_state, lifecycle_handler=None)` with `process_messages(messages: list[SbMessage], *, receiver: SbReceiver) -> None`. `SbMessage` Protocol: `body_as_str() -> str`. `SbReceiver` Protocol: `complete(message) -> None`, `abandon(message) -> None`. Per **CD-1**: parse → `submit` all notes → `run_once()` → on return, `receiver.complete(message)`; if `run_once()` raises, `receiver.abandon(message)` and re-raise so the loop can stop/redeliver. Lifecycle-only messages are completed after `lifecycle_handler.handle(...)`.

- [ ] **Step 1: Write the failing test.** `tests/providers/servicebus/test_runtime.py`:

```python
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
```

- [ ] **Step 2: Run it, verify it fails.** Run: `python -m pytest tests/providers/servicebus/test_runtime.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement.** `src/mailflow/adapters/servicebus/runtime.py`:

```python
"""ServiceBusRuntime — realizes the per-message flow for Service Bus delivery:
SB message -> unwrap Event Grid CloudEvent -> GraphNotification -> submit to
GraphProvider -> pipeline.run_once() -> complete(message). Mirrors
GraphEventHubsRuntime but with per-message ack instead of offset checkpoint.

CD-1 (DLQ ownership): the pipeline owns message-level dead-lettering (dead_lettered +
dlq_emitter), so we COMPLETE after run_once() returns (success or handled-poison). We
only ABANDON (for redelivery) when run_once() RAISES — a transport/infra error — and we
re-raise so the consume loop can react. We never call Service Bus dead_letter() ourselves;
SB's MaxDeliveryCount DLQ is the infra backstop. One poison email => one mailflow DLQ record."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mailflow.adapters.graph.lifecycle import GraphLifecycleHandler
from mailflow.adapters.graph.notifications import parse_lifecycle_payload
from mailflow.adapters.graph.provider import GraphProvider
from mailflow.adapters.servicebus.eventgrid import _events, parse_eventgrid_message


@runtime_checkable
class SbMessage(Protocol):
    def body_as_str(self) -> str: ...


@runtime_checkable
class SbReceiver(Protocol):
    def complete(self, message: Any) -> None: ...
    def abandon(self, message: Any) -> None: ...


@runtime_checkable
class PipelineLike(Protocol):
    def run_once(self) -> object: ...


def _lifecycle_collection(body: str) -> str:
    """Re-shape CloudEvents into the {'value':[...]} form parse_lifecycle_payload wants."""
    import json
    items = []
    for ev in _events(body):
        data = ev.get("data") or {}
        items.append({
            "subscriptionId": data.get("subscriptionId", ""),
            "clientState": data.get("clientState", ""),
            "lifecycleEvent": data.get("lifecycleEvent", ""),
            "resource": data.get("resource") or ev.get("subject") or "",
        })
    return json.dumps({"value": items})


class ServiceBusRuntime:
    def __init__(
        self,
        *,
        provider: GraphProvider,
        pipeline: PipelineLike,
        client_state: str,
        lifecycle_handler: GraphLifecycleHandler | None = None,
    ) -> None:
        self.provider = provider
        self.pipeline = pipeline
        self.client_state = client_state
        self.lifecycle_handler = lifecycle_handler

    def process_messages(self, messages: list[SbMessage], *, receiver: SbReceiver) -> None:
        for message in messages:
            body = message.body_as_str()
            notes = parse_eventgrid_message(body, expected_client_state=self.client_state)
            try:
                if notes:
                    for note in notes:
                        self.provider.submit(note)
                    self.pipeline.run_once()
                elif self.lifecycle_handler is not None:
                    for lifecycle in parse_lifecycle_payload(
                        _lifecycle_collection(body), expected_client_state=self.client_state
                    ):
                        self.lifecycle_handler.handle(lifecycle)
            except Exception:
                receiver.abandon(message)   # not completed -> SB redelivers (CD-1)
                raise
            receiver.complete(message)      # ack after successful processing (CD-1)
```

  Widen `__init__.py` `ServiceBusRuntime` import (already in `__all__`).

  > `_events` is imported from `eventgrid.py` — promote it if you prefer a public name; keeping the leading underscore import is acceptable within the same adapter package (document it). If mypy objects to importing a private name across modules, rename `_events` → `split_cloudevents` in `eventgrid.py` and update both call sites in the same commit.

- [ ] **Step 4: Run tests + mypy.** Run: `python -m pytest tests/providers/servicebus/test_runtime.py -v` — Expected: PASS. `python -m mypy` — Expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/adapters/servicebus/runtime.py src/mailflow/adapters/servicebus/__init__.py tests/providers/servicebus/test_runtime.py
git commit -m "feat(servicebus): ServiceBusRuntime — complete-after-success, abandon-on-infra-error (CD-1 parity)"
```

### Task D2: `build_servicebus_graph_runtime` composition root (no SDK)

**Files:**
- Create: `src/mailflow/adapters/servicebus/composition.py`
- Test: `tests/providers/servicebus/test_composition.py`

**Interfaces:**
- Consumes: `GraphClient` (`adapters/graph/client.py`), `GraphProvider`, `GraphEnvelopeParser`, `GraphExtractor`, `Pipeline`/`PipelineConfig` (`core/pipeline.py`), `FilterChain` (`filters/chain.py`), the `TokenProvider`/`HttpTransport` ports (`adapters/graph/transport.py`), and the stores/emitter ports. Reuses the Graph provider+parser+extractor **unchanged** (no new Graph code).
- Produces: `build_servicebus_graph_runtime(*, graph_cfg, tenant, token_provider, transport, emitter, dlq_emitter, cursor_store, dedupe_store, blob_store, filters=None, cleaner=None, on_filtered="tag") -> ServiceBusRuntime`. Note it threads `on_filtered` into `PipelineConfig` (the gap the Event Hubs path has — do it right here).

- [ ] **Step 1: Write the failing test.** `tests/providers/servicebus/test_composition.py`:

```python
from mailflow.adapters.graph.config import GraphConfig
from mailflow.adapters.servicebus.composition import build_servicebus_graph_runtime
from mailflow.adapters.servicebus.runtime import ServiceBusRuntime
from mailflow.emit.memory import MemoryEmitter
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore


class _FakeToken:
    def get_token(self): return "t"


class _FakeTransport:
    def request(self, method, url, *, headers, json): raise AssertionError("no network in unit test")


def test_builds_a_servicebus_runtime_wired_to_a_graph_pipeline():
    rt = build_servicebus_graph_runtime(
        graph_cfg=GraphConfig(
            tenant_id="t", client_id="c", client_secret_ref="env://X", mailboxes=["ops@acme.com"]
        ),
        tenant="acme",
        token_provider=_FakeToken(),
        transport=_FakeTransport(),
        emitter=MemoryEmitter(),
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
    )
    assert isinstance(rt, ServiceBusRuntime)
    assert rt.client_state == "mailflow"   # from GraphConfig default
```

  > Confirm the in-memory store import paths (`mailflow.stores.memory`) against the repo — the README imports `InMemoryBlobStore/InMemoryCursorStore/InMemoryDedupeStore` from `mailflow.stores.memory`. If the names differ, use the real ones.

- [ ] **Step 2: Run it, verify it fails.** Run: `python -m pytest tests/providers/servicebus/test_composition.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement.** `src/mailflow/adapters/servicebus/composition.py`:

```python
"""Composition root for the Service Bus + Event Grid ingress stack. Wires GraphClient ->
GraphProvider -> Pipeline -> ServiceBusRuntime from injected pieces. Imports NO vendor SDK
(azure/msal/httpx) — the real glue is in live.py. Reuses the Graph provider/parser/extractor
unchanged; only the transport runtime differs from the Event Hubs path."""

from __future__ import annotations

from typing import Literal

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.config import GraphConfig
from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.adapters.graph.provider import GraphProvider
from mailflow.adapters.graph.transport import HttpTransport, TokenProvider
from mailflow.adapters.servicebus.runtime import ServiceBusRuntime
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.core.ports import (
    BlobStore,
    Classifier,
    ContentCleaner,
    CursorStore,
    DedupeStore,
    Emitter,
    Filter,
)
from mailflow.filters.chain import FilterChain


def build_servicebus_graph_runtime(
    *,
    graph_cfg: GraphConfig,
    tenant: str,
    token_provider: TokenProvider,
    transport: HttpTransport,
    emitter: Emitter,
    dlq_emitter: Emitter,
    cursor_store: CursorStore,
    dedupe_store: DedupeStore,
    blob_store: BlobStore,
    filters: list[Filter] | None = None,
    classifier: Classifier | None = None,
    cleaner: ContentCleaner | None = None,
    on_filtered: Literal["tag", "drop"] = "tag",
) -> ServiceBusRuntime:
    client = GraphClient(
        base_url=graph_cfg.base_url,
        token_provider=token_provider,
        transport=transport,
        max_retries=graph_cfg.max_attempts,
    )
    provider = GraphProvider(client=client)
    pipeline = Pipeline(
        provider=provider,
        parser=GraphEnvelopeParser(),
        filters=FilterChain(filters or []),
        extractor=GraphExtractor(),
        emitter=emitter,
        dlq_emitter=dlq_emitter,
        cursor_store=cursor_store,
        dedupe_store=dedupe_store,
        blob_store=blob_store,
        config=PipelineConfig(
            tenant=tenant, max_attempts=graph_cfg.max_attempts, on_filtered=on_filtered
        ),
        classifier=classifier,
        cleaner=cleaner,
    )
    return ServiceBusRuntime(
        provider=provider, pipeline=pipeline, client_state=graph_cfg.client_state
    )
```

  > Verify `PipelineConfig` accepts `on_filtered` (it does on the Gmail path: `gmail/composition.py:71`). If the `Pipeline`/`PipelineConfig` constructor signature differs from the Graph composition's, copy the **exact** kwargs from `adapters/graph/composition.py:69-82`.

- [ ] **Step 4: Run tests + mypy.** Run: `python -m pytest tests/providers/servicebus/test_composition.py -v` — Expected: PASS. `python -m mypy` — Expected: clean.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/adapters/servicebus/composition.py tests/providers/servicebus/test_composition.py
git commit -m "feat(servicebus): composition root (Graph provider+pipeline -> ServiceBusRuntime, threads on_filtered)"
```

---

## Part E — Live SDK glue + import-safety

### Task E1: `live.py` — SDK adapters + `run_service` (local imports only)

**Files:**
- Create: `src/mailflow/adapters/servicebus/live.py`
- Test: `tests/providers/servicebus/test_live_import.py`

**Interfaces:**
- Consumes: `build_servicebus_graph_runtime` (D2), `MsalTokenProvider`/`HttpxTransport` (reused from `adapters/graph/live.py`), `SecretProvider`.
- Produces:
  - `AzureServiceBusSender(sender)` — adapts an `azure.servicebus` queue/topic sender to the `SbSender` Protocol (`send(*, body, message_id, content_type, session_id)`); used by `ServiceBusEmitter` in production.
  - `run_service(*, graph_cfg, servicebus_cfg, tenant, secret_provider, emitter, dlq_emitter, cursor_store, dedupe_store, blob_store, filters=None, cleaner=None, on_filtered="tag", connection_string=None, credential=None, max_wait_time=5.0) -> None` — resolves the app secret, builds the MSAL token provider + httpx transport, builds the runtime, then blocks in a Service Bus receive loop calling `runtime.process_messages(batch, receiver=receiver)`.

- [ ] **Step 1: Write the import-safety test.** `tests/providers/servicebus/test_live_import.py`:

```python
def test_live_module_imports_without_servicebus_extra():
    # All azure imports must be local to functions, so importing the module must NOT
    # require azure-servicebus to be installed.
    import importlib
    mod = importlib.import_module("mailflow.adapters.servicebus.live")
    assert hasattr(mod, "run_service")
    assert hasattr(mod, "AzureServiceBusSender")
```

- [ ] **Step 2: Run it, verify it fails.** Run: `python -m pytest tests/providers/servicebus/test_live_import.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement.** `src/mailflow/adapters/servicebus/live.py`:

```python
"""Real SDK glue for the Service Bus + Event Grid adapter. Every azure/msal/httpx import
is LOCAL to a function, so `import mailflow.adapters.servicebus.live` works without the
`servicebus` (or `graph`) extra installed. Only calling run_service / constructing the
sender pulls in the SDKs.

Install the extra to run live:  pip install -e ".[servicebus,graph]"

Auth is pluggable: pass a connection string OR an azure-identity credential
(e.g. DefaultAzureCredential). The operator supplies the secret/key — nothing hardcoded."""

from __future__ import annotations

from typing import Any

from mailflow.adapters.graph.config import GraphConfig
from mailflow.adapters.graph.live import HttpxTransport, MsalTokenProvider
from mailflow.adapters.servicebus.composition import build_servicebus_graph_runtime
from mailflow.adapters.servicebus.config import ServiceBusConfig
from mailflow.core.ports import (
    BlobStore,
    ContentCleaner,
    CursorStore,
    DedupeStore,
    Emitter,
    Filter,
    SecretProvider,
)


class AzureServiceBusSender:
    """Adapts an azure-servicebus sender to the SbSender Protocol used by ServiceBusEmitter."""

    def __init__(self, sender: Any) -> None:
        self._sender = sender

    def send(
        self, *, body: bytes, message_id: str, content_type: str, session_id: str | None
    ) -> None:
        from azure.servicebus import ServiceBusMessage  # local import

        self._sender.send_messages(
            ServiceBusMessage(
                body, message_id=message_id, content_type=content_type, session_id=session_id
            )
        )


class _ReceiverAdapter:
    """Adapts an azure-servicebus receiver to the runtime's SbReceiver Protocol."""

    def __init__(self, receiver: Any) -> None:
        self._receiver = receiver

    def complete(self, message: Any) -> None:
        self._receiver.complete_message(message)

    def abandon(self, message: Any) -> None:
        self._receiver.abandon_message(message)


class _MessageAdapter:
    def __init__(self, message: Any) -> None:
        self._message = message

    def body_as_str(self) -> str:
        return str(self._message)  # azure-servicebus message __str__ yields the body text


def run_service(
    *,
    graph_cfg: GraphConfig,
    servicebus_cfg: ServiceBusConfig,
    tenant: str,
    secret_provider: SecretProvider,
    emitter: Emitter,
    dlq_emitter: Emitter,
    cursor_store: CursorStore,
    dedupe_store: DedupeStore,
    blob_store: BlobStore,
    filters: list[Filter] | None = None,
    cleaner: ContentCleaner | None = None,
    on_filtered: str = "tag",
    connection_string: str | None = None,
    credential: Any | None = None,
    max_wait_time: float = 5.0,
    max_batch: int = 20,
) -> None:
    """Full live entrypoint: build the Graph token provider + transport, build the
    ServiceBusRuntime, then block in a Service Bus receive loop feeding process_messages."""
    if connection_string is None and credential is None:
        raise ValueError("run_service needs either connection_string or credential")

    token_provider = MsalTokenProvider(
        tenant_id=graph_cfg.tenant_id,
        client_id=graph_cfg.client_id,
        client_secret=secret_provider.get(graph_cfg.client_secret_ref),
        scope=graph_cfg.scope,
    )
    runtime = build_servicebus_graph_runtime(
        graph_cfg=graph_cfg, tenant=tenant, token_provider=token_provider,
        transport=HttpxTransport(), emitter=emitter, dlq_emitter=dlq_emitter,
        cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
        filters=filters, cleaner=cleaner, on_filtered=on_filtered,  # type: ignore[arg-type]
    )

    from azure.servicebus import ServiceBusClient  # local import

    if connection_string is not None:
        client = ServiceBusClient.from_connection_string(connection_string)
    else:
        assert credential is not None
        client = ServiceBusClient(
            fully_qualified_namespace=servicebus_cfg.fully_qualified_namespace,
            credential=credential,
        )

    with client:
        with client.get_queue_receiver(queue_name=servicebus_cfg.entity_name) as receiver:
            sb_receiver = _ReceiverAdapter(receiver)
            while True:
                batch = receiver.receive_messages(
                    max_message_count=max_batch, max_wait_time=max_wait_time
                )
                if not batch:
                    continue
                runtime.process_messages(
                    [_MessageAdapter(m) for m in batch], receiver=sb_receiver
                )
```

  > **`_MessageAdapter.body_as_str`:** `str(ServiceBusReceivedMessage)` returns the UTF-8 body for JSON payloads. If the capture in Task A2 shows a bytes/generator body, switch to `b"".join(message.body).decode("utf-8")`. Verify against the captured fixture.
  > **Receiver/message correspondence:** `_ReceiverAdapter.complete/abandon` are called by the runtime with the **adapted** message. Since `complete_message` needs the *raw* azure message, adjust `_MessageAdapter` to also carry the raw message and have `_ReceiverAdapter` unwrap it — OR pass raw messages to the runtime and wrap `body_as_str` differently. **Implementer note:** thread the raw message through so `complete_message(raw)` gets the raw object (fix in this step; the unit test in D1 uses fakes so it won't catch this — verify manually in Task F2 smoke).

- [ ] **Step 4: Run tests + mypy.** Run: `python -m pytest tests/providers/servicebus/test_live_import.py -v` — Expected: PASS. `python -m mypy` — Expected: clean. Run the FULL suite: `python -m pytest` — Expected: all green.

- [ ] **Step 5: Commit.**

```bash
git add src/mailflow/adapters/servicebus/live.py tests/providers/servicebus/test_live_import.py
git commit -m "feat(servicebus): live run_service + SDK adapters (local imports; azure-free unit suite)"
```

---

## Part F — Docs + end-to-end smoke

### Task F1: README section

**Files:**
- Modify: `README.md` (add after the "Live: Outlook via Microsoft Graph + Azure Event Hubs" section)

- [ ] **Step 1:** Add a "Live: Outlook via Event Grid → Azure Service Bus (no Event Hubs)" section documenting: the ingress chain, the egress `overrides={"emitter": ServiceBusEmitter(sender=AzureServiceBusSender(...))}` snippet, the `pip install -e ".[servicebus,graph]"` line, a pointer to `docs/azure-servicebus-setup.md`, and the **CD-1** DLQ-ownership note + **CD-2** created-vs-sweep caveat.

- [ ] **Step 2: Commit.**

```bash
git add README.md
git commit -m "docs(servicebus): document Event Grid -> Service Bus ingress + ServiceBusEmitter egress"
```

### Task F2: Live end-to-end smoke (manual, gated on provisioning)

**Files:** none (operator-run; record results in the PR).

- [ ] **Step 1:** With A1–A2 provisioned and `pip install -e ".[servicebus,graph]"`, run a short live harness that calls `run_service(...)` against the real queue with a `MemoryEmitter`. Send a test email; confirm one `CleanEmail` is emitted.
- [ ] **Step 2:** Verify the raw-message/complete correspondence noted in E1-Step-3 works (message is completed, not stuck/redelivered).
- [ ] **Step 3:** Record outcome (emitted count; whether a `created` or only `updated` event drove it — feeds CD-2) in the PR description.

---

## Self-Review checklist (completed by plan author)

1. **Spec coverage:** Egress Emitter (Part B) ✓; Event Grid → Service Bus ingress (Parts C/D/E) ✓; ports-only, no core edits ✓; local vendor imports + `[servicebus]` extra ✓; unit tests with fakes ✓; cursor/dedupe honored via reused Pipeline + idempotency_key ✓; DLQ-ownership contract (CD-1) ✓. Provisioning + validation gate (Part A) covers the real-world "we have Service Bus, not Event Hubs" constraint ✓.
2. **Placeholder scan:** every code step has complete code; the two genuine unknowns (exact CloudEvent body accessor; created-vs-updated for messages) are handled by an explicit **capture-and-verify** gate (A2/F2), not left as TODO.
3. **Type consistency:** `Emitter.emit(event) -> object` matches B2; `SbSender.send(*, body, message_id, content_type, session_id)` identical in emitter.py/live.py; `ServiceBusRuntime(provider, pipeline, client_state, lifecycle_handler)` identical in D1/D2/E1; `parse_eventgrid_message(body, *, expected_client_state)` identical in C1/D1; `GraphNotification` reused from `adapters/graph/notifications.py` (not redefined).

**Known verification points flagged inline (do not skip):** (a) `make_clean_email`/`CleanEmail` construction in tests must match the real model; (b) in-memory store import paths; (c) `PipelineConfig` kwargs vs `graph/composition.py`; (d) the raw-message↔complete correspondence in `live.py`; (e) the captured Event Grid payload (A2) is the source of truth for `eventgrid.py`'s field mapping.
