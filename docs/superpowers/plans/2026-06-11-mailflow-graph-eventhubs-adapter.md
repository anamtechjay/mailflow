# Mailflow Graph + Event Hubs Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build mailflow's first real provider adapter — Microsoft Graph (Outlook/Exchange Online), app-only — where change notifications are delivered **directly to Azure Event Hubs (no public webhook)**, and the runtime re-fetches each message from Graph and runs it through the existing mailflow pipeline to emit a `CleanEmail`.

**Architecture:** Ports & adapters. All Graph/Azure code lives under `src/mailflow/adapters/graph/` and plugs into the frozen `core/ports.py` Protocols without touching `core`. The adapter is built around **injected internal Protocols** (`TokenProvider`, `HttpTransport`, `EventHubReceiver`, `Checkpointer`) so every unit test runs against fakes with **zero network and zero real SDK calls**. The real SDK glue (MSAL, httpx, azure-eventhub) is a thin, separately-wired layer that the unit suite does not import. Runtime flow (per the `eventhub_runtime_flow_per_email.svg`): new mail → Graph publishes a thin pointer to the Event Hub → consumer validates + extracts ids → `GraphProvider` re-fetches the full message by id (with `internetMessageId` fallback) → pipeline parses, filters, extracts, emits.

**Tech Stack:** Python 3.12+, pydantic v2 (config + models), stdlib `json`/`base64`/`email.utils`, pytest (mocked fakes), mypy strict. Live extras (not needed for the unit suite): `msal`, `httpx`, `azure-eventhub`, `azure-eventhub-checkpointstoreblob-aio`.

**Design decisions locked from research** (`docs/graph-connection.md`, `docs/graph-eventhubs-delivery.md`):
- **Delivery:** Event Hubs direct (Option 1). `notificationUrl = EventHub:https://<ns>.servicebus.windows.net/eventhubname/<hub>?tenantId=<domain>`. No webhook, no validation handshake to answer (skip the `subscriptionId=="NA"` event). Graph writes as service principal **`0bf30f3b-4a52-48df-9a82-234910c4a086`** granted **Azure Event Hubs Data Sender**.
- **Notification mode:** **basic + re-fetch** (id-only pointer; full message fetched later). Rich/encrypted kept as a config-only switch for later — NOT implemented here.
- **Folders:** **Inbox + Sent** per mailbox (`mailFolders('inbox')`, `mailFolders('sentitems')`), config-driven.
- **Subscription lifetime:** message basic = **10,080 min (7 days)** max; renew on a timer; **recreate on 404**.
- **Dedupe:** on `internetMessageId` (Event Hubs has no broker dedup) folded into mailflow's `idempotency_key`.
- **Extractor seam (frozen contract, `CLAUDE.md`):** `GraphExtractor` implements the port `extract(msg, env) -> CleanEmail` directly (NOT `extract_bytes`); `Pipeline._extract` already dispatches to the port method for any non-`MimeExtractor`.
- **Carrying Graph JSON through core unchanged:** `GraphProvider.fetch` puts the fetched Graph message JSON (with embedded attachment metadata under `"_attachments"`) into `RawMessage.raw_bytes` as UTF-8 JSON. `GraphEnvelopeParser` and `GraphExtractor` `json.loads(msg.raw_bytes)`. This avoids changing the frozen `RawMessage` model.
- **Attachments:** metadata only in this plan (filename/content_type/size/contentId/isInline) — **no blob streaming yet** (deferred, matches spec §8.6 "size guard ships before streaming"). `storage_ref`/`content_hash` stay empty.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/mailflow/adapters/__init__.py` | empty package marker |
| `src/mailflow/adapters/graph/__init__.py` | curated adapter exports |
| `src/mailflow/adapters/graph/config.py` | `GraphConfig`, `EventHubConfig`, `GraphFolder` (pydantic) |
| `src/mailflow/adapters/graph/notifications.py` | `GraphNotification` model + `parse_notification_payload` (skip NA, verify clientState) |
| `src/mailflow/adapters/graph/transport.py` | internal Protocols: `HttpResponse`, `HttpTransport`, `TokenProvider`; `GraphError` |
| `src/mailflow/adapters/graph/client.py` | `GraphClient` over `HttpTransport` — get message, list attachments, subscription CRUD, 429/Retry-After retry |
| `src/mailflow/adapters/graph/subscriptions.py` | `GraphSubscriptionManager` (implements `SubscriptionManager`) + lifecycle handling |
| `src/mailflow/adapters/graph/parser.py` | `GraphEnvelopeParser` (implements `EnvelopeParser`) — Graph JSON → `Envelope` |
| `src/mailflow/adapters/graph/extractor.py` | `GraphExtractor` (implements `ContentExtractor.extract`) — Graph JSON → `CleanEmail` |
| `src/mailflow/adapters/graph/provider.py` | `GraphProvider` (implements `MailboxProvider`) — notification-fed; fetch by id + `internetMessageId` fallback; `message_size` from metadata |
| `src/mailflow/adapters/graph/runtime.py` | `GraphEventHubsRuntime` — drains Event Hub events → provider → `pipeline.run_once()` → checkpoint; `Checkpointer`/`EventHubReceiver` Protocols |
| `tests/adapters/graph/…` | one test module per source module + an end-to-end mocked flow test |

> **Note (this machine):** the `.venv` path in `CLAUDE.md` is from another host. Run the suite here with `python -m pytest --import-mode=importlib`. All commands below assume that.

---

## Task 0: Adapter package scaffold

**Files:**
- Create: `src/mailflow/adapters/__init__.py` (empty)
- Create: `src/mailflow/adapters/graph/__init__.py` (empty for now)
- Create: `tests/adapters/__init__.py` (empty)
- Create: `tests/adapters/graph/__init__.py` (empty)
- Test: `tests/adapters/graph/test_package.py`

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_package.py`:
```python
def test_graph_adapter_package_imports():
    import mailflow.adapters.graph as g
    assert g is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_package.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters'`.

- [ ] **Step 3: Create the empty package files**

Create all four `__init__.py` files listed above as empty files.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_package.py -v --import-mode=importlib`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters tests/adapters
git commit -m "feat(graph): scaffold adapters.graph package"
```

---

## Task 1: Config models

**Files:**
- Create: `src/mailflow/adapters/graph/config.py`
- Test: `tests/adapters/graph/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_config.py`:
```python
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig


def test_graph_config_defaults_and_folders():
    cfg = GraphConfig.model_validate({
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "22222222-2222-2222-2222-222222222222",
        "client_secret_ref": "kv://graph-client-secret",
        "mailboxes": ["ops@acme.com"],
    })
    assert cfg.base_url == "https://graph.microsoft.com/v1.0"
    assert cfg.scope == "https://graph.microsoft.com/.default"
    assert cfg.folders == ["inbox", "sentitems"]          # Inbox + Sent default
    assert cfg.delivery == "eventhub"
    assert cfg.max_attempts == 3


def test_eventhub_config_builds_notification_url():
    eh = EventHubConfig.model_validate({
        "namespace": "evh-graphevents",
        "hub": "graph-notifications",
        "tenant_domain": "acme.com",
        "consumer_group": "$Default",
    })
    assert eh.notification_url == (
        "EventHub:https://evh-graphevents.servicebus.windows.net/"
        "eventhubname/graph-notifications?tenantId=acme.com"
    )


def test_graph_config_requires_at_least_one_mailbox():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GraphConfig.model_validate({
            "tenant_id": "t", "client_id": "c",
            "client_secret_ref": "r", "mailboxes": [],
        })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_config.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters.graph.config'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/adapters/graph/config.py`:
```python
"""Typed config for the Graph + Event Hubs adapter. Secrets are *references*
resolved later via the SecretProvider port — never literal secrets here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class EventHubConfig(BaseModel):
    namespace: str                      # e.g. "evh-graphevents" (no .servicebus suffix)
    hub: str                            # e.g. "graph-notifications"
    tenant_domain: str                  # primary domain, e.g. "acme.com"
    consumer_group: str = "$Default"
    checkpoint_blob_ref: str = ""       # ref to blob checkpoint store conn (live wiring)

    @property
    def notification_url(self) -> str:
        # RBAC form (SAS is deprecated): Graph publishes here as the
        # Change Tracking SP granted "Azure Event Hubs Data Sender".
        return (
            f"EventHub:https://{self.namespace}.servicebus.windows.net/"
            f"eventhubname/{self.hub}?tenantId={self.tenant_domain}"
        )


class GraphConfig(BaseModel):
    tenant_id: str
    client_id: str
    client_secret_ref: str              # SecretProvider ref, NOT the secret
    mailboxes: list[str]                # user ids / UPNs to watch
    folders: list[str] = Field(default_factory=lambda: ["inbox", "sentitems"])
    base_url: str = "https://graph.microsoft.com/v1.0"
    scope: str = "https://graph.microsoft.com/.default"
    delivery: Literal["eventhub", "webhook"] = "eventhub"
    client_state: str = "mailflow"      # subscription tripwire (≤128 chars)
    subscription_minutes: int = 8640    # ~6 days; under the 10,080 (7d) max
    max_attempts: int = 3

    @field_validator("mailboxes")
    @classmethod
    def _at_least_one_mailbox(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("at least one mailbox is required")
        return v

    @field_validator("client_state")
    @classmethod
    def _client_state_len(cls, v: str) -> str:
        if len(v) > 128:
            raise ValueError("client_state must be <= 128 chars")
        return v
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_config.py -v --import-mode=importlib`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/config.py tests/adapters/graph/test_config.py
git commit -m "feat(graph): config models (GraphConfig, EventHubConfig)"
```

---

## Task 2: Notification parsing (Event Hub payload → thin pointer)

**Files:**
- Create: `src/mailflow/adapters/graph/notifications.py`
- Test: `tests/adapters/graph/test_notifications.py`

This parses the Event Hub event body (a Graph `changeNotificationCollection`) into typed pointers, skipping the `subscriptionId=="NA"` validation event and dropping items whose `clientState` does not match.

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_notifications.py`:
```python
import json

from mailflow.adapters.graph.notifications import (
    GraphNotification,
    parse_notification_payload,
)

REAL = json.dumps({"value": [{
    "subscriptionId": "sub-1",
    "changeType": "created",
    "clientState": "secret",
    "resource": "users/ops@acme.com/messages/AAA",
    "tenantId": "t",
    "resourceData": {"@odata.type": "#Microsoft.Graph.Message", "id": "AAA"},
}]})

VALIDATION = json.dumps({"value": [{
    "subscriptionId": "NA", "changeType": "Validation: Testing...",
    "clientState": "NA", "resource": "NA",
    "resourceData": {"@odata.type": "NA", "@odata.id": "NA", "id": "NA"},
}]})


def test_parses_real_notification():
    notes = parse_notification_payload(REAL, expected_client_state="secret")
    assert len(notes) == 1
    n = notes[0]
    assert isinstance(n, GraphNotification)
    assert n.user_id == "ops@acme.com"
    assert n.message_id == "AAA"
    assert n.change_type == "created"
    assert n.subscription_id == "sub-1"


def test_skips_validation_event():
    assert parse_notification_payload(VALIDATION, expected_client_state="secret") == []


def test_drops_items_with_wrong_client_state():
    assert parse_notification_payload(REAL, expected_client_state="WRONG") == []


def test_extracts_user_and_message_id_from_resource_variants():
    payload = json.dumps({"value": [{
        "subscriptionId": "s", "changeType": "created", "clientState": "secret",
        "resource": "Users/abc-123@acme.com/Messages/ZZZ",
        "resourceData": {"id": "ZZZ"},
    }]})
    notes = parse_notification_payload(payload, expected_client_state="secret")
    assert notes[0].user_id == "abc-123@acme.com"
    assert notes[0].message_id == "ZZZ"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_notifications.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters.graph.notifications'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/adapters/graph/notifications.py`:
```python
"""Parse an Event Hub event body (Graph changeNotificationCollection) into thin
pointers. Skips the validation event (subscriptionId == 'NA') and drops any item
whose clientState does not match (constant-time compare)."""

from __future__ import annotations

import hmac
import json
import re
from typing import Any

from pydantic import BaseModel

_RESOURCE_RE = re.compile(r"users/(?P<user>[^/]+)/messages/(?P<msg>[^/?]+)", re.IGNORECASE)


class GraphNotification(BaseModel):
    subscription_id: str
    change_type: str
    user_id: str
    message_id: str


def _user_and_message(item: dict[str, Any]) -> tuple[str, str] | None:
    resource = str(item.get("resource", ""))
    m = _RESOURCE_RE.search(resource)
    msg_id = str((item.get("resourceData") or {}).get("id", "")) or (m.group("msg") if m else "")
    if not m and not msg_id:
        return None
    user = m.group("user") if m else ""
    if not user or not msg_id:
        return None
    return user, msg_id


def parse_notification_payload(body: str, *, expected_client_state: str) -> list[GraphNotification]:
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    out: list[GraphNotification] = []
    for item in data.get("value", []):
        if str(item.get("subscriptionId")) == "NA":
            continue  # validation event — ignore per Event Hubs delivery docs
        if not hmac.compare_digest(str(item.get("clientState", "")), expected_client_state):
            continue  # forged / mismatched — drop, never log clientState
        ids = _user_and_message(item)
        if ids is None:
            continue
        user, msg = ids
        out.append(GraphNotification(
            subscription_id=str(item.get("subscriptionId", "")),
            change_type=str(item.get("changeType", "")),
            user_id=user,
            message_id=msg,
        ))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_notifications.py -v --import-mode=importlib`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/notifications.py tests/adapters/graph/test_notifications.py
git commit -m "feat(graph): parse Event Hub notification payload into thin pointers"
```

---

## Task 3: Internal transport Protocols + fake

**Files:**
- Create: `src/mailflow/adapters/graph/transport.py`
- Test: `tests/adapters/graph/test_transport.py`

Defines the seam that makes the adapter testable without a real HTTP client: `HttpResponse`, `HttpTransport`, `TokenProvider`, and the `GraphError` exception. Also ships a `FakeTransport` (in the source tree, under a `testing` submodule) reused by later tests.

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_transport.py`:
```python
from mailflow.adapters.graph.transport import GraphError
from mailflow.adapters.graph.testing import FakeTransport, FakeToken


def test_fake_token_returns_configured_value():
    assert FakeToken("abc").get_token() == "abc"


def test_fake_transport_serves_queued_responses_and_records_calls():
    t = FakeTransport()
    t.enqueue(200, {"id": "AAA"})
    resp = t.request("GET", "https://graph/x", headers={"Authorization": "Bearer abc"}, json=None)
    assert resp.status_code == 200
    assert resp.json() == {"id": "AAA"}
    assert t.calls[0].method == "GET"
    assert t.calls[0].url.endswith("/x")


def test_graph_error_carries_status():
    err = GraphError(404, "not found")
    assert err.status_code == 404
    assert "404" in str(err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_transport.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters.graph.transport'`.

- [ ] **Step 3: Write the implementations**

`src/mailflow/adapters/graph/transport.py`:
```python
"""Internal seams so the adapter is testable without a real HTTP client / MSAL."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class GraphError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"graph error {status_code}: {message}")


@runtime_checkable
class HttpResponse(Protocol):
    status_code: int
    def json(self) -> Any: ...
    @property
    def headers(self) -> dict[str, str]: ...
    @property
    def content(self) -> bytes: ...


@runtime_checkable
class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, *, headers: dict[str, str], json: Any | None
    ) -> HttpResponse: ...


@runtime_checkable
class TokenProvider(Protocol):
    def get_token(self) -> str: ...
```

`src/mailflow/adapters/graph/testing.py`:
```python
"""Test doubles for the Graph adapter. Imported by tests AND usable in demos."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class FakeToken:
    def __init__(self, value: str = "fake-token") -> None:
        self._value = value

    def get_token(self) -> str:
        return self._value


@dataclass
class _Resp:
    status_code: int
    _json: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""

    def json(self) -> Any:
        return self._json


@dataclass
class _Call:
    method: str
    url: str
    headers: dict[str, str]
    json: Any


class FakeTransport:
    """Serves queued responses FIFO; records every call."""

    def __init__(self) -> None:
        self._queue: list[_Resp] = []
        self.calls: list[_Call] = []

    def enqueue(
        self, status: int, body: Any = None, *,
        headers: dict[str, str] | None = None, content: bytes = b"",
    ) -> None:
        self._queue.append(_Resp(status, body, headers or {}, content))

    def request(
        self, method: str, url: str, *, headers: dict[str, str], json: Any | None
    ) -> _Resp:
        self.calls.append(_Call(method, url, headers, json))
        if not self._queue:
            raise AssertionError(f"no fake response queued for {method} {url}")
        return self._queue.pop(0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_transport.py -v --import-mode=importlib`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/transport.py src/mailflow/adapters/graph/testing.py tests/adapters/graph/test_transport.py
git commit -m "feat(graph): internal transport protocols + test doubles"
```

---

## Task 4: GraphClient — messages, attachments, 429 retry

**Files:**
- Create: `src/mailflow/adapters/graph/client.py`
- Test: `tests/adapters/graph/test_client.py`

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_client.py`:
```python
from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.testing import FakeToken, FakeTransport


def _client(t: FakeTransport) -> GraphClient:
    return GraphClient(
        base_url="https://graph.microsoft.com/v1.0",
        token_provider=FakeToken("tok"),
        transport=t,
        max_retries=2,
    )


def test_get_message_selects_fields_and_sends_bearer():
    t = FakeTransport()
    t.enqueue(200, {"id": "AAA", "subject": "hi"})
    msg = _client(t).get_message("ops@acme.com", "AAA")
    assert msg["subject"] == "hi"
    call = t.calls[0]
    assert call.method == "GET"
    assert "/users/ops@acme.com/messages/AAA" in call.url
    assert "$select=" in call.url and "internetMessageId" in call.url
    assert call.headers["Authorization"] == "Bearer tok"


def test_get_message_by_internet_id_uses_filter():
    t = FakeTransport()
    t.enqueue(200, {"value": [{"id": "NEW", "subject": "moved"}]})
    msg = _client(t).get_message_by_internet_id("ops@acme.com", "<rfc@x>")
    assert msg["id"] == "NEW"
    assert "$filter=internetMessageId%20eq%20" in t.calls[0].url or \
           "$filter=internetMessageId eq " in t.calls[0].url


def test_get_message_by_internet_id_returns_none_when_empty():
    t = FakeTransport()
    t.enqueue(200, {"value": []})
    assert _client(t).get_message_by_internet_id("ops@acme.com", "<x>") is None


def test_list_attachments_returns_value_array():
    t = FakeTransport()
    t.enqueue(200, {"value": [{"id": "att1", "name": "q.pdf", "size": 10}]})
    atts = _client(t).list_attachments("ops@acme.com", "AAA")
    assert atts[0]["name"] == "q.pdf"


def test_retries_on_429_then_succeeds():
    t = FakeTransport()
    t.enqueue(429, {}, headers={"Retry-After": "0"})
    t.enqueue(200, {"id": "AAA"})
    msg = _client(t).get_message("ops@acme.com", "AAA")
    assert msg["id"] == "AAA"
    assert len(t.calls) == 2


def test_raises_graph_error_on_404():
    import pytest
    from mailflow.adapters.graph.transport import GraphError
    t = FakeTransport()
    t.enqueue(404, {"error": {"message": "gone"}})
    with pytest.raises(GraphError) as ei:
        _client(t).get_message("ops@acme.com", "AAA")
    assert ei.value.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_client.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters.graph.client'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/adapters/graph/client.py`:
```python
"""Thin Graph REST client over an injected HttpTransport. Handles auth header,
the $select field list, the internetMessageId fallback, and 429/Retry-After
retries. No real HTTP library here — that is the transport's job."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

from mailflow.adapters.graph.transport import GraphError, HttpResponse, HttpTransport, TokenProvider

MESSAGE_SELECT = (
    "id,internetMessageId,subject,from,sender,toRecipients,ccRecipients,"
    "bccRecipients,replyTo,body,uniqueBody,bodyPreview,isDraft,hasAttachments,"
    "receivedDateTime,sentDateTime,conversationId,parentFolderId,"
    "internetMessageHeaders,categories"
)


class GraphClient:
    def __init__(
        self, *, base_url: str, token_provider: TokenProvider,
        transport: HttpTransport, max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = token_provider
        self.transport = transport
        self.max_retries = max_retries

    def _request(self, method: str, url: str, *, json: Any | None = None) -> HttpResponse:
        attempt = 0
        while True:
            headers = {
                "Authorization": f"Bearer {self.tokens.get_token()}",
                "Content-Type": "application/json",
            }
            resp = self.transport.request(method, url, headers=headers, json=json)
            if resp.status_code == 429 and attempt < self.max_retries:
                retry_after = float(resp.headers.get("Retry-After", "1"))
                time.sleep(retry_after)
                attempt += 1
                continue
            if resp.status_code >= 400:
                msg = ""
                try:
                    msg = str((resp.json() or {}).get("error", {}).get("message", ""))
                except Exception:  # noqa: BLE001 - error body may not be JSON
                    msg = ""
                raise GraphError(resp.status_code, msg)
            return resp

    def get_message(self, user_id: str, message_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/users/{user_id}/messages/{message_id}?$select={MESSAGE_SELECT}"
        result = self._request("GET", url).json()
        assert isinstance(result, dict)
        return result

    def get_message_by_internet_id(self, user_id: str, internet_id: str) -> dict[str, Any] | None:
        flt = quote(f"internetMessageId eq '{internet_id}'")
        url = f"{self.base_url}/users/{user_id}/messages?$filter={flt}&$select={MESSAGE_SELECT}"
        data = self._request("GET", url).json()
        items = (data or {}).get("value", []) if isinstance(data, dict) else []
        return items[0] if items else None

    def list_attachments(self, user_id: str, message_id: str) -> list[dict[str, Any]]:
        url = (f"{self.base_url}/users/{user_id}/messages/{message_id}/attachments"
               f"?$select=id,name,contentType,size,isInline,contentId")
        data = self._request("GET", url).json()
        items = (data or {}).get("value", []) if isinstance(data, dict) else []
        return [a for a in items if isinstance(a, dict)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_client.py -v --import-mode=importlib`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/client.py tests/adapters/graph/test_client.py
git commit -m "feat(graph): GraphClient (messages, attachments, 429 retry, internetMessageId fallback)"
```

---

## Task 5: GraphClient subscription CRUD

**Files:**
- Modify: `src/mailflow/adapters/graph/client.py` (add subscription methods)
- Test: `tests/adapters/graph/test_client_subscriptions.py`

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_client_subscriptions.py`:
```python
from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.testing import FakeToken, FakeTransport


def _client(t: FakeTransport) -> GraphClient:
    return GraphClient(base_url="https://graph.microsoft.com/v1.0",
                       token_provider=FakeToken(), transport=t)


def test_create_subscription_posts_eventhub_url():
    t = FakeTransport()
    t.enqueue(201, {"id": "sub-1", "expirationDateTime": "2026-06-17T00:00:00Z"})
    out = _client(t).create_subscription(
        resource="users/ops@acme.com/mailFolders('inbox')/messages",
        notification_url="EventHub:https://ns.servicebus.windows.net/eventhubname/h?tenantId=acme.com",
        client_state="secret",
        expiration_iso="2026-06-17T00:00:00Z",
    )
    assert out["id"] == "sub-1"
    call = t.calls[0]
    assert call.method == "POST" and call.url.endswith("/subscriptions")
    assert call.json["changeType"] == "created,updated"
    assert call.json["notificationUrl"].startswith("EventHub:")
    assert call.json["lifecycleNotificationUrl"].startswith("EventHub:")


def test_renew_subscription_patches_expiration():
    t = FakeTransport()
    t.enqueue(200, {"id": "sub-1", "expirationDateTime": "2026-06-18T00:00:00Z"})
    out = _client(t).renew_subscription("sub-1", "2026-06-18T00:00:00Z")
    assert out["expirationDateTime"].startswith("2026-06-18")
    assert t.calls[0].method == "PATCH" and t.calls[0].url.endswith("/subscriptions/sub-1")


def test_delete_subscription_calls_delete():
    t = FakeTransport()
    t.enqueue(204, None)
    _client(t).delete_subscription("sub-1")
    assert t.calls[0].method == "DELETE" and t.calls[0].url.endswith("/subscriptions/sub-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_client_subscriptions.py -v --import-mode=importlib`
Expected: FAIL with `AttributeError: 'GraphClient' object has no attribute 'create_subscription'`.

- [ ] **Step 3: Add the methods to `GraphClient`**

Append these methods inside the `GraphClient` class in `src/mailflow/adapters/graph/client.py`:
```python
    def create_subscription(
        self, *, resource: str, notification_url: str,
        client_state: str, expiration_iso: str,
    ) -> dict[str, Any]:
        body = {
            "changeType": "created,updated",
            "resource": resource,
            "notificationUrl": notification_url,
            "lifecycleNotificationUrl": notification_url,
            "clientState": client_state,
            "expirationDateTime": expiration_iso,
        }
        result = self._request("POST", f"{self.base_url}/subscriptions", json=body).json()
        assert isinstance(result, dict)
        return result

    def renew_subscription(self, subscription_id: str, expiration_iso: str) -> dict[str, Any]:
        url = f"{self.base_url}/subscriptions/{subscription_id}"
        result = self._request("PATCH", url, json={"expirationDateTime": expiration_iso}).json()
        assert isinstance(result, dict)
        return result

    def delete_subscription(self, subscription_id: str) -> None:
        self._request("DELETE", f"{self.base_url}/subscriptions/{subscription_id}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_client_subscriptions.py -v --import-mode=importlib`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/client.py tests/adapters/graph/test_client_subscriptions.py
git commit -m "feat(graph): subscription create/renew/delete on GraphClient"
```

---

## Task 6: GraphSubscriptionManager (implements SubscriptionManager)

**Files:**
- Create: `src/mailflow/adapters/graph/subscriptions.py`
- Test: `tests/adapters/graph/test_subscriptions.py`

Implements the frozen `SubscriptionManager` port (`ensure_watch`, `renew_watch`) and adds adapter-internal `recreate-on-404`. The handle is the Graph subscription id.

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_subscriptions.py`:
```python
from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.graph.subscriptions import GraphSubscriptionManager
from mailflow.adapters.graph.testing import FakeToken, FakeTransport
from mailflow.core.models import StreamRef


def _mgr(t: FakeTransport) -> GraphSubscriptionManager:
    cfg = GraphConfig.model_validate({
        "tenant_id": "t", "client_id": "c", "client_secret_ref": "r",
        "mailboxes": ["ops@acme.com"], "client_state": "secret",
        "subscription_minutes": 8640,
    })
    eh = EventHubConfig(namespace="ns", hub="h", tenant_domain="acme.com")
    client = GraphClient(base_url="https://graph.microsoft.com/v1.0",
                         token_provider=FakeToken(), transport=t)
    return GraphSubscriptionManager(client=client, config=cfg, eventhub=eh, now_iso=lambda: "2026-06-11T00:00:00Z")


def test_ensure_watch_creates_subscription_for_inbox_stream():
    t = FakeTransport()
    t.enqueue(201, {"id": "sub-1"})
    handle = _mgr(t).ensure_watch(StreamRef(mailbox="ops@acme.com", folder="inbox"))
    assert handle == "sub-1"
    assert t.calls[0].json["resource"] == "users/ops@acme.com/mailFolders('inbox')/messages"
    assert t.calls[0].json["notificationUrl"].startswith("EventHub:")


def test_renew_watch_patches_and_returns_same_handle():
    t = FakeTransport()
    t.enqueue(200, {"id": "sub-1"})
    assert _mgr(t).renew_watch("sub-1") == "sub-1"
    assert t.calls[0].method == "PATCH"


def test_renew_watch_recreates_on_404():
    from mailflow.adapters.graph.transport import GraphError
    t = FakeTransport()
    t.enqueue(404, {"error": {"message": "gone"}})   # PATCH fails
    # recreate path needs the stream; manager remembers it from ensure_watch
    mgr = _mgr(t)
    t.enqueue(201, {"id": "sub-1"})                    # initial ensure_watch
    handle = mgr.ensure_watch(StreamRef(mailbox="ops@acme.com", folder="inbox"))
    t.enqueue(201, {"id": "sub-2"})                    # recreate after 404
    new_handle = mgr.renew_watch(handle)
    assert new_handle == "sub-2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_subscriptions.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters.graph.subscriptions'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/adapters/graph/subscriptions.py`:
```python
"""GraphSubscriptionManager — implements the core SubscriptionManager port for
Event Hubs delivery. ensure_watch creates a per-folder subscription whose
notificationUrl points at the Event Hub; renew_watch PATCHes the expiry and
recreates the subscription if Graph returns 404 (subscription gone)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.graph.transport import GraphError
from mailflow.core.models import StreamRef


def _default_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GraphSubscriptionManager:
    def __init__(
        self, *, client: GraphClient, config: GraphConfig, eventhub: EventHubConfig,
        now_iso: Callable[[], str] = _default_now_iso,
    ) -> None:
        self.client = client
        self.config = config
        self.eventhub = eventhub
        self._now_iso = now_iso
        self._stream_by_handle: dict[str, StreamRef] = {}

    def _resource(self, stream: StreamRef) -> str:
        folder = stream.folder or "inbox"
        return f"users/{stream.mailbox}/mailFolders('{folder}')/messages"

    def _expiration_iso(self) -> str:
        base = datetime.strptime(self._now_iso(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        exp = base + timedelta(minutes=self.config.subscription_minutes)
        return exp.strftime("%Y-%m-%dT%H:%M:%SZ")

    def ensure_watch(self, stream: StreamRef) -> object:
        out = self.client.create_subscription(
            resource=self._resource(stream),
            notification_url=self.eventhub.notification_url,
            client_state=self.config.client_state,
            expiration_iso=self._expiration_iso(),
        )
        handle = str(out["id"])
        self._stream_by_handle[handle] = stream
        return handle

    def renew_watch(self, handle: object) -> object:
        sub_id = str(handle)
        try:
            self.client.renew_subscription(sub_id, self._expiration_iso())
            return sub_id
        except GraphError as exc:
            if exc.status_code != 404:
                raise
            stream = self._stream_by_handle.get(sub_id)
            if stream is None:
                raise
            new_handle = self.ensure_watch(stream)
            self._stream_by_handle.pop(sub_id, None)
            return new_handle
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_subscriptions.py -v --import-mode=importlib`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/subscriptions.py tests/adapters/graph/test_subscriptions.py
git commit -m "feat(graph): GraphSubscriptionManager with Event Hub URL + recreate-on-404"
```

---

## Task 7: GraphEnvelopeParser (Graph JSON → Envelope)

**Files:**
- Create: `src/mailflow/adapters/graph/parser.py`
- Test: `tests/adapters/graph/test_parser.py`

Reads the Graph message JSON carried in `RawMessage.raw_bytes` and produces a cheap `Envelope` for the filter stage. No network.

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_parser.py`:
```python
import json
from datetime import datetime, timezone

from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.core.models import Cursor, RawMessage, StreamRef

GRAPH_MSG = {
    "id": "AAA", "internetMessageId": "<abc@x>", "subject": "Quote request",
    "from": {"emailAddress": {"name": "Alice", "address": "alice@partner.com"}},
    "toRecipients": [{"emailAddress": {"name": "Ops", "address": "ops@acme.com"}}],
    "ccRecipients": [], "bodyPreview": "Hello please send a quote",
    "receivedDateTime": "2026-06-11T10:00:00Z", "sentDateTime": "2026-06-11T09:59:00Z",
    "internetMessageHeaders": [
        {"name": "List-Id", "value": "<news.partner.com>"},
        {"name": "Auto-Submitted", "value": "no"},
    ],
}


def _raw() -> RawMessage:
    return RawMessage(
        provider="graph", provider_message_id="AAA",
        stream=StreamRef(mailbox="ops@acme.com", folder="inbox"),
        size_bytes=100, received_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        cursor=Cursor(value="c1", order=1),
        raw_bytes=json.dumps(GRAPH_MSG).encode("utf-8"),
    )


def test_parses_envelope_from_graph_json():
    env = GraphEnvelopeParser().parse_envelope(_raw(), tenant="acme")
    assert env.subject == "Quote request"
    assert env.from_.address == "alice@partner.com"
    assert env.from_.name == "Alice"
    assert env.to[0].address == "ops@acme.com"
    assert env.canonical_id == "<abc@x>"
    assert env.message_id_trusted is True
    assert env.list_id == "<news.partner.com>"
    assert env.snippet.startswith("Hello")
    assert env.provider == "graph"
    assert env.provider_message_id == "AAA"
    assert env.stream.key == "ops@acme.com:inbox"


def test_untrusted_when_no_internet_message_id():
    msg = {k: v for k, v in GRAPH_MSG.items() if k != "internetMessageId"}
    raw = _raw().model_copy(update={"raw_bytes": json.dumps(msg).encode("utf-8")})
    env = GraphEnvelopeParser().parse_envelope(raw, tenant="acme")
    assert env.message_id_trusted is False
    assert env.canonical_id  # always present (stable hash fallback)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_parser.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters.graph.parser'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/adapters/graph/parser.py`:
```python
"""GraphEnvelopeParser — Graph message JSON (carried in RawMessage.raw_bytes) to
a provider-neutral Envelope. Implements the EnvelopeParser port. No network."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mailflow.core.identity import derive_canonical_id
from mailflow.core.models import Envelope, RawMessage, Recipient, StreamRef


def _recipient(node: dict[str, Any] | None) -> Recipient:
    ea = (node or {}).get("emailAddress", {}) if node else {}
    return Recipient(name=str(ea.get("name", "")), address=str(ea.get("address", "")))


def _recipients(nodes: list[dict[str, Any]] | None) -> list[Recipient]:
    return [_recipient(n) for n in (nodes or []) if (n or {}).get("emailAddress", {}).get("address")]


def _headers(msg: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for h in msg.get("internetMessageHeaders", []) or []:
        name = str(h.get("name", "")).lower()
        if name:
            out.setdefault(name, []).append(str(h.get("value", "")))
    return out


def _first(headers: dict[str, list[str]], name: str) -> str | None:
    vals = headers.get(name)
    return vals[0] if vals else None


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class GraphEnvelopeParser:
    def parse_envelope(self, msg: RawMessage, tenant: str) -> Envelope:
        data: dict[str, Any] = json.loads(msg.raw_bytes or b"{}")
        message_id = data.get("internetMessageId")
        canonical_id, present, trusted = derive_canonical_id(
            provider=msg.provider, provider_message_id=msg.provider_message_id,
            mailbox=msg.stream.mailbox, message_id=message_id,
        )
        headers = _headers(data)
        env_kwargs: dict[str, Any] = {
            "canonical_id": canonical_id,
            "message_id": message_id,
            "message_id_present": present,
            "message_id_trusted": trusted,
            "provider": msg.provider,
            "provider_message_id": msg.provider_message_id,
            "stream": msg.stream,
            "from": _recipient(data.get("from")),
            "sender": _recipient(data.get("sender")) if data.get("sender") else None,
            "reply_to": _recipient((data.get("replyTo") or [None])[0]) if data.get("replyTo") else None,
            "to": _recipients(data.get("toRecipients")),
            "cc": _recipients(data.get("ccRecipients")),
            "subject": str(data.get("subject", "")),
            "date_utc": _dt(data.get("sentDateTime")),
            "received_at": _dt(data.get("receivedDateTime")),
            "snippet": str(data.get("bodyPreview", "")),
            "list_id": _first(headers, "list-id"),
            "list_unsubscribe": _first(headers, "list-unsubscribe"),
            "auto_submitted": _first(headers, "auto-submitted"),
            "headers": headers,
        }
        return Envelope.model_validate(env_kwargs)
```

> Note: `Envelope` uses the `from`↔`from_` alias (`populate_by_name=True`), so passing the dict key `"from"` is correct.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_parser.py -v --import-mode=importlib`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/parser.py tests/adapters/graph/test_parser.py
git commit -m "feat(graph): GraphEnvelopeParser (Graph JSON -> Envelope)"
```

---

## Task 8: GraphExtractor (Graph JSON → CleanEmail)

**Files:**
- Create: `src/mailflow/adapters/graph/extractor.py`
- Test: `tests/adapters/graph/test_extractor.py`

Implements `ContentExtractor.extract(msg, env) -> CleanEmail` (the port method — this is the seam the pipeline dispatches to for non-MIME extractors). Reads the Graph JSON + the embedded `"_attachments"` metadata from `raw_bytes`.

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_extractor.py`:
```python
import json
from datetime import datetime, timezone

from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.core.models import Cursor, Direction, RawMessage, StreamRef

GRAPH_MSG = {
    "id": "AAA", "internetMessageId": "<abc@x>", "subject": "Quote",
    "from": {"emailAddress": {"name": "Alice", "address": "alice@partner.com"}},
    "toRecipients": [{"emailAddress": {"address": "ops@acme.com"}}],
    "body": {"contentType": "html", "content": "<p>hi</p>"},
    "uniqueBody": {"contentType": "html", "content": "<p>hi</p>"},
    "receivedDateTime": "2026-06-11T10:00:00Z", "sentDateTime": "2026-06-11T09:59:00Z",
    "isDraft": False, "categories": ["Deals"], "conversationId": "conv-1",
    "internetMessageHeaders": [{"name": "In-Reply-To", "value": "<prev@x>"}],
    "_attachments": [
        {"id": "att1", "name": "q.pdf", "contentType": "application/pdf",
         "size": 1234, "isInline": False, "contentId": None},
        {"id": "att2", "name": "logo.png", "contentType": "image/png",
         "size": 50, "isInline": True, "contentId": "<logo>"},
    ],
}


def _raw() -> RawMessage:
    return RawMessage(
        provider="graph", provider_message_id="AAA",
        stream=StreamRef(mailbox="ops@acme.com", folder="inbox"),
        size_bytes=1284, received_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
        cursor=Cursor(value="c1", order=1),
        raw_bytes=json.dumps(GRAPH_MSG).encode("utf-8"),
    )


def test_extracts_clean_email_fields():
    raw = _raw()
    env = GraphEnvelopeParser().parse_envelope(raw, tenant="acme")
    ce = GraphExtractor().extract(raw, env)
    assert ce.canonical_id == "<abc@x>"
    assert ce.subject == "Quote"
    assert ce.from_.address == "alice@partner.com"
    assert ce.to[0].address == "ops@acme.com"
    assert ce.body_html == "<p>hi</p>"
    assert ce.direction is Direction.inbound
    assert ce.categories == ["Deals"]
    assert ce.in_reply_to == "<prev@x>"
    assert ce.schema_version == "1.0"


def test_separates_real_and_inline_attachments():
    raw = _raw()
    env = GraphEnvelopeParser().parse_envelope(raw, tenant="acme")
    ce = GraphExtractor().extract(raw, env)
    reals = [a for a in ce.attachments if not a.is_inline]
    inlines = [a for a in ce.attachments if a.is_inline]
    assert len(reals) == 1 and reals[0].filename == "q.pdf" and reals[0].size_bytes == 1234
    assert len(inlines) == 1 and inlines[0].content_id == "<logo>"


def test_outbound_direction_when_from_is_watched_mailbox():
    msg = dict(GRAPH_MSG)
    msg["from"] = {"emailAddress": {"address": "ops@acme.com"}}
    raw = _raw().model_copy(update={"raw_bytes": json.dumps(msg).encode("utf-8")})
    env = GraphEnvelopeParser().parse_envelope(raw, tenant="acme")
    ce = GraphExtractor().extract(raw, env)
    assert ce.direction is Direction.outbound
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_extractor.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters.graph.extractor'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/adapters/graph/extractor.py`:
```python
"""GraphExtractor — Graph message JSON (+ embedded _attachments metadata) to a
CleanEmail. Implements the ContentExtractor port's extract(msg, env) method, the
seam Pipeline._extract dispatches to for non-MIME extractors. No network: the
provider has already fetched the JSON and attachment metadata into raw_bytes.
Attachment BYTES are not downloaded here (metadata only; streaming deferred)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mailflow.core.events import SCHEMA_VERSION
from mailflow.core.models import Attachment, CleanEmail, Direction, Envelope, RawMessage, Recipient


def _recipient(node: dict[str, Any] | None) -> Recipient:
    ea = (node or {}).get("emailAddress", {}) if node else {}
    return Recipient(name=str(ea.get("name", "")), address=str(ea.get("address", "")))


def _recipients(nodes: list[dict[str, Any]] | None) -> list[Recipient]:
    return [_recipient(n) for n in (nodes or []) if (n or {}).get("emailAddress", {}).get("address")]


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _body(data: dict[str, Any]) -> tuple[str, str]:
    body = data.get("body") or {}
    content = str(body.get("content", ""))
    if str(body.get("contentType", "")).lower() == "html":
        return "", content
    return content, ""


def _attachments(data: dict[str, Any]) -> list[Attachment]:
    out: list[Attachment] = []
    for a in data.get("_attachments", []) or []:
        is_inline = bool(a.get("isInline")) or bool(a.get("contentId"))
        out.append(Attachment(
            filename=str(a.get("name", "")),
            content_type=str(a.get("contentType", "application/octet-stream")),
            size_bytes=int(a.get("size", 0) or 0),
            content_id=str(a.get("contentId") or ""),
            is_inline=is_inline,
            provider_attachment_id=str(a.get("id", "")),
        ))
    return out


class GraphExtractor:
    def extract(self, msg: RawMessage, env: Envelope) -> CleanEmail:
        data: dict[str, Any] = json.loads(msg.raw_bytes or b"{}")
        body_text, body_html = _body(data)
        from_ = _recipient(data.get("from"))
        direction = (
            Direction.outbound
            if from_.address.lower() == msg.stream.mailbox.lower()
            else Direction.inbound
        )
        headers = env.headers
        ce_kwargs: dict[str, Any] = {
            "canonical_id": env.canonical_id,
            "message_id": env.message_id,
            "message_id_present": env.message_id_present,
            "message_id_trusted": env.message_id_trusted,
            "in_reply_to": (headers.get("in-reply-to") or [None])[0],
            "references": (headers.get("references") or [""])[0].split() if headers.get("references") else [],
            "provider": msg.provider,
            "provider_message_id": msg.provider_message_id,
            "provider_stream_id": msg.stream.key,
            "direction": direction,
            "is_draft": bool(data.get("isDraft", False)),
            "from": from_,
            "sender": _recipient(data.get("sender")) if data.get("sender") else None,
            "reply_to": _recipient((data.get("replyTo") or [None])[0]) if data.get("replyTo") else None,
            "to": _recipients(data.get("toRecipients")),
            "cc": _recipients(data.get("ccRecipients")),
            "bcc": _recipients(data.get("bccRecipients")),
            "subject": str(data.get("subject", "")),
            "date_utc": _dt(data.get("sentDateTime")),
            "received_at": _dt(data.get("receivedDateTime")),
            "body_text": body_text,
            "body_html": body_html,
            "attachments": _attachments(data),
            "categories": [str(c) for c in data.get("categories", []) or []],
            "folder": msg.stream.folder or "",
            "list_id": env.list_id,
            "list_unsubscribe": env.list_unsubscribe,
            "auto_submitted": env.auto_submitted,
            "message_size_bytes": msg.size_bytes,
            "raw_headers": headers,
            "schema_version": SCHEMA_VERSION,
        }
        return CleanEmail.model_validate(ce_kwargs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_extractor.py -v --import-mode=importlib`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/extractor.py tests/adapters/graph/test_extractor.py
git commit -m "feat(graph): GraphExtractor (Graph JSON -> CleanEmail, port extract seam)"
```

---

## Task 9: GraphProvider (notification-fed MailboxProvider)

**Files:**
- Create: `src/mailflow/adapters/graph/provider.py`
- Test: `tests/adapters/graph/test_provider.py`

Implements `MailboxProvider`. It is **notification-fed**: `submit(notification)` queues a pointer; `fetch(stream, cursor)` does the Graph GET per pending pointer (with `internetMessageId` fallback when the id 404s after a folder move), embeds attachment metadata under `"_attachments"`, and yields a `RawMessage` carrying the JSON. `message_size` reads attachment sizes + body length from that JSON.

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_provider.py`:
```python
import json

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.notifications import GraphNotification
from mailflow.adapters.graph.provider import GraphProvider
from mailflow.adapters.graph.testing import FakeToken, FakeTransport
from mailflow.core.models import StreamRef


def _provider(t: FakeTransport) -> GraphProvider:
    client = GraphClient(base_url="https://graph.microsoft.com/v1.0",
                         token_provider=FakeToken(), transport=t)
    return GraphProvider(client=client)


def test_fetch_gets_message_and_embeds_attachments():
    t = FakeTransport()
    t.enqueue(200, {"id": "AAA", "internetMessageId": "<a@x>", "subject": "hi",
                    "hasAttachments": True, "body": {"contentType": "text", "content": "hello"}})
    t.enqueue(200, {"value": [{"id": "att1", "name": "q.pdf", "size": 10, "isInline": False}]})
    p = _provider(t)
    p.submit(GraphNotification(subscription_id="s", change_type="created",
                               user_id="ops@acme.com", message_id="AAA"))
    p.connect()
    stream = StreamRef(mailbox="ops@acme.com", folder="inbox")
    msgs = list(p.fetch(stream, None))
    assert len(msgs) == 1
    data = json.loads(msgs[0].raw_bytes)
    assert data["subject"] == "hi"
    assert data["_attachments"][0]["name"] == "q.pdf"
    assert msgs[0].provider == "graph"
    assert msgs[0].provider_message_id == "AAA"


def test_fetch_falls_back_to_internet_message_id_on_404():
    from mailflow.adapters.graph.transport import GraphError  # noqa: F401
    t = FakeTransport()
    t.enqueue(404, {"error": {"message": "moved"}})                 # GET by id fails
    t.enqueue(200, {"value": [{"id": "NEW", "internetMessageId": "<a@x>",
                               "subject": "moved", "hasAttachments": False}]})  # by internetMessageId
    p = _provider(t)
    p.submit(GraphNotification(subscription_id="s", change_type="updated",
                               user_id="ops@acme.com", message_id="OLD",
                               ))
    # internet id is learned from the change feed in real life; here we pass it
    p.submit_internet_id("OLD", "<a@x>")
    p.connect()
    msgs = list(p.fetch(StreamRef(mailbox="ops@acme.com", folder="inbox"), None))
    assert json.loads(msgs[0].raw_bytes)["id"] == "NEW"


def test_sync_streams_returns_streams_with_pending_work():
    t = FakeTransport()
    p = _provider(t)
    p.submit(GraphNotification(subscription_id="s", change_type="created",
                               user_id="ops@acme.com", message_id="AAA"))
    streams = list(p.sync_streams())
    assert StreamRef(mailbox="ops@acme.com", folder="inbox") in streams


def test_message_size_sums_attachments_and_body():
    t = FakeTransport()
    p = _provider(t)
    from mailflow.core.models import Cursor, RawMessage
    data = {"body": {"content": "hello"}, "_attachments": [{"size": 1000}, {"size": 200}]}
    raw = RawMessage(provider="graph", provider_message_id="AAA",
                     stream=StreamRef(mailbox="ops@acme.com", folder="inbox"),
                     size_bytes=0, received_at=__import__("datetime").datetime(2026, 6, 11),
                     cursor=Cursor(value="c", order=1),
                     raw_bytes=json.dumps(data).encode())
    assert p.message_size(raw) == 1205  # 1000 + 200 + len("hello")
```

> Note: this test passes a naive `datetime` to keep the fixture short; `RawMessage`
> accepts it. The real provider always sets a tz-aware `received_at`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_provider.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters.graph.provider'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/adapters/graph/provider.py`:
```python
"""GraphProvider — notification-fed MailboxProvider. The Event Hubs runtime feeds
pointers via submit(); fetch() does the actual Graph GET per pending pointer and
yields RawMessages carrying the message JSON (with attachment metadata embedded
under '_attachments'). This keeps the core RawMessage shape unchanged."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable, Iterator

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.notifications import GraphNotification
from mailflow.adapters.graph.transport import GraphError
from mailflow.core.models import Cursor, RawMessage, StreamRef

_FOLDER_DISPLAY = {"inbox": "inbox", "sentitems": "sentitems"}


class GraphProvider:
    PROVIDER = "graph"

    def __init__(self, *, client: GraphClient) -> None:
        self.client = client
        self._pending: list[GraphNotification] = []
        self._internet_ids: dict[str, str] = {}
        self._order = 0

    # --- notification feed (called by the Event Hubs runtime) ---
    def submit(self, note: GraphNotification) -> None:
        self._pending.append(note)

    def submit_internet_id(self, message_id: str, internet_id: str) -> None:
        self._internet_ids[message_id] = internet_id

    def _stream_for(self, note: GraphNotification) -> StreamRef:
        # Folder is not in the basic notification; default to inbox. (A future
        # refinement resolves parentFolderId after fetch; inbox is the v1 default.)
        return StreamRef(mailbox=note.user_id, folder="inbox")

    # --- MailboxProvider port ---
    def connect(self) -> None:
        return None

    def sync_streams(self) -> Iterable[StreamRef]:
        seen: list[StreamRef] = []
        for note in self._pending:
            s = self._stream_for(note)
            if s not in seen:
                seen.append(s)
        return seen

    def fetch(self, stream: StreamRef, cursor: Cursor | None) -> Iterator[RawMessage]:
        remaining: list[GraphNotification] = []
        for note in self._pending:
            if self._stream_for(note) != stream:
                remaining.append(note)
                continue
            data = self._fetch_message(note)
            if data is None:
                continue
            yield self._to_raw(stream, note, data)
        self._pending = remaining

    def message_size(self, msg: RawMessage) -> int | None:
        try:
            data = json.loads(msg.raw_bytes or b"{}")
        except (ValueError, TypeError):
            return msg.size_bytes
        body = (data.get("body") or {}).get("content", "")
        att = sum(int(a.get("size", 0) or 0) for a in data.get("_attachments", []) or [])
        return att + len(str(body))

    # --- internals ---
    def _fetch_message(self, note: GraphNotification) -> dict[str, object] | None:
        try:
            data = self.client.get_message(note.user_id, note.message_id)
        except GraphError as exc:
            if exc.status_code != 404:
                raise
            internet_id = self._internet_ids.get(note.message_id)
            if not internet_id:
                return None
            data = self.client.get_message_by_internet_id(note.user_id, internet_id)
            if data is None:
                return None
        if data.get("hasAttachments"):
            data["_attachments"] = self.client.list_attachments(
                note.user_id, str(data.get("id", note.message_id))
            )
        else:
            data["_attachments"] = []
        return data

    def _to_raw(self, stream: StreamRef, note: GraphNotification, data: dict[str, object]) -> RawMessage:
        self._order += 1
        raw_bytes = json.dumps(data).encode("utf-8")
        return RawMessage(
            provider=self.PROVIDER,
            provider_message_id=str(data.get("id", note.message_id)),
            stream=stream,
            size_bytes=len(raw_bytes),
            received_at=datetime.now(timezone.utc),
            cursor=Cursor(value=f"{stream.key}#{self._order}", order=self._order),
            raw_bytes=raw_bytes,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_provider.py -v --import-mode=importlib`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/provider.py tests/adapters/graph/test_provider.py
git commit -m "feat(graph): GraphProvider (notification-fed fetch + internetMessageId fallback)"
```

---

## Task 10: GraphEventHubsRuntime (drain events → provider → pipeline → checkpoint)

**Files:**
- Create: `src/mailflow/adapters/graph/runtime.py`
- Test: `tests/adapters/graph/test_runtime.py`

The glue that realizes the SVG flow. Defines `EventHubReceiver` and `Checkpointer` Protocols (faked in tests; real azure-eventhub wiring is separate). `process_batch` parses each event body into pointers, submits them to the `GraphProvider`, runs `pipeline.run_once()`, and checkpoints after success.

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_runtime.py`:
```python
import json
from dataclasses import dataclass

from mailflow.adapters.graph.runtime import GraphEventHubsRuntime


@dataclass
class FakeEvent:
    body: str
    def body_as_str(self) -> str:
        return self.body


class FakeProvider:
    def __init__(self) -> None:
        self.submitted = []
    def submit(self, note) -> None:  # noqa: ANN001
        self.submitted.append(note)


class FakePipeline:
    def __init__(self) -> None:
        self.runs = 0
    def run_once(self):  # noqa: ANN201
        self.runs += 1
        return object()


class FakeCheckpointer:
    def __init__(self) -> None:
        self.checkpoints = []
    def update(self, event) -> None:  # noqa: ANN001
        self.checkpoints.append(event)


REAL = json.dumps({"value": [{
    "subscriptionId": "s", "changeType": "created", "clientState": "secret",
    "resource": "users/ops@acme.com/messages/AAA", "resourceData": {"id": "AAA"},
}]})
VALIDATION = json.dumps({"value": [{"subscriptionId": "NA", "clientState": "NA",
                                    "resource": "NA", "resourceData": {"id": "NA"}}]})


def _runtime(provider, pipeline, ckpt):  # noqa: ANN001
    return GraphEventHubsRuntime(provider=provider, pipeline=pipeline,
                                 checkpointer=ckpt, client_state="secret")


def test_real_event_submits_pointer_runs_pipeline_and_checkpoints():
    provider, pipeline, ckpt = FakeProvider(), FakePipeline(), FakeCheckpointer()
    rt = _runtime(provider, pipeline, ckpt)
    rt.process_batch([FakeEvent(REAL)])
    assert len(provider.submitted) == 1 and provider.submitted[0].message_id == "AAA"
    assert pipeline.runs == 1
    assert len(ckpt.checkpoints) == 1


def test_validation_event_is_checkpointed_without_running_pipeline():
    provider, pipeline, ckpt = FakeProvider(), FakePipeline(), FakeCheckpointer()
    rt = _runtime(provider, pipeline, ckpt)
    rt.process_batch([FakeEvent(VALIDATION)])
    assert provider.submitted == []
    assert pipeline.runs == 0
    assert len(ckpt.checkpoints) == 1   # still advance past the validation event


def test_pipeline_failure_does_not_checkpoint():
    class Boom(FakePipeline):
        def run_once(self):  # noqa: ANN201
            raise RuntimeError("downstream down")
    provider, pipeline, ckpt = FakeProvider(), Boom(), FakeCheckpointer()
    rt = _runtime(provider, pipeline, ckpt)
    import pytest
    with pytest.raises(RuntimeError):
        rt.process_batch([FakeEvent(REAL)])
    assert ckpt.checkpoints == []   # not checkpointed -> event redelivered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_runtime.py -v --import-mode=importlib`
Expected: FAIL with `ModuleNotFoundError: No module named 'mailflow.adapters.graph.runtime'`.

- [ ] **Step 3: Write the implementation**

`src/mailflow/adapters/graph/runtime.py`:
```python
"""GraphEventHubsRuntime — realizes the per-email flow: Event Hub event ->
parse pointer -> submit to GraphProvider -> pipeline.run_once() -> checkpoint.
Defines minimal Protocols so the real azure-eventhub SDK stays out of the unit
suite. Checkpoint happens only after the pipeline run succeeds, so a failed run
leaves the event for redelivery."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mailflow.adapters.graph.notifications import parse_notification_payload
from mailflow.adapters.graph.provider import GraphProvider


@runtime_checkable
class EventHubEvent(Protocol):
    def body_as_str(self) -> str: ...


@runtime_checkable
class Checkpointer(Protocol):
    def update(self, event: EventHubEvent) -> None: ...


@runtime_checkable
class PipelineLike(Protocol):
    def run_once(self) -> object: ...


class GraphEventHubsRuntime:
    def __init__(
        self, *, provider: GraphProvider, pipeline: PipelineLike,
        checkpointer: Checkpointer, client_state: str,
    ) -> None:
        self.provider = provider
        self.pipeline = pipeline
        self.checkpointer = checkpointer
        self.client_state = client_state

    def process_batch(self, events: list[EventHubEvent]) -> None:
        for event in events:
            notes = parse_notification_payload(
                event.body_as_str(), expected_client_state=self.client_state
            )
            if notes:
                for note in notes:
                    self.provider.submit(note)
                self.pipeline.run_once()
            # checkpoint AFTER successful processing (or for ignored validation
            # events, which produce no notes) so failures get redelivered.
            self.checkpointer.update(event)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_runtime.py -v --import-mode=importlib`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mailflow/adapters/graph/runtime.py tests/adapters/graph/test_runtime.py
git commit -m "feat(graph): GraphEventHubsRuntime (event -> provider -> pipeline -> checkpoint)"
```

---

## Task 11: End-to-end mocked flow + adapter exports

**Files:**
- Modify: `src/mailflow/adapters/graph/__init__.py` (curated exports)
- Test: `tests/adapters/graph/test_end_to_end.py`

Proves the full SVG flow with fakes only: an Event Hub event → runtime → provider re-fetch (mocked Graph) → real mailflow `Pipeline` (memory stores + memory emitter) → an emitted `CleanEmail`.

- [ ] **Step 1: Write the failing test**

`tests/adapters/graph/test_end_to_end.py`:
```python
import json
from dataclasses import dataclass

from mailflow.adapters.graph import (
    GraphClient, GraphEnvelopeParser, GraphEventHubsRuntime, GraphExtractor, GraphProvider,
)
from mailflow.adapters.graph.testing import FakeToken, FakeTransport
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.filters.chain import FilterChain
from mailflow.stores.memory import InMemoryBlobStore, InMemoryCursorStore, InMemoryDedupeStore


@dataclass
class FakeEvent:
    body: str
    def body_as_str(self) -> str:
        return self.body


class ListCheckpointer:
    def __init__(self) -> None:
        self.seen = []
    def update(self, event) -> None:  # noqa: ANN001
        self.seen.append(event)


NOTIFICATION = json.dumps({"value": [{
    "subscriptionId": "sub-1", "changeType": "created", "clientState": "secret",
    "resource": "users/ops@acme.com/messages/AAA", "resourceData": {"id": "AAA"},
}]})

GRAPH_MSG = {
    "id": "AAA", "internetMessageId": "<abc@x>", "subject": "Quote request",
    "from": {"emailAddress": {"address": "alice@partner.com"}},
    "toRecipients": [{"emailAddress": {"address": "ops@acme.com"}}],
    "body": {"contentType": "text", "content": "please send a quote"},
    "hasAttachments": False,
    "receivedDateTime": "2026-06-11T10:00:00Z", "sentDateTime": "2026-06-11T09:59:00Z",
}


def test_event_hub_event_flows_to_emitted_clean_email():
    transport = FakeTransport()
    transport.enqueue(200, GRAPH_MSG)            # GraphProvider.fetch -> get_message
    client = GraphClient(base_url="https://graph.microsoft.com/v1.0",
                         token_provider=FakeToken(), transport=transport)
    provider = GraphProvider(client=client)
    emitter = MemoryEmitter()
    pipeline = Pipeline(
        provider=provider,
        parser=GraphEnvelopeParser(),
        filters=FilterChain([]),
        extractor=GraphExtractor(),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )
    runtime = GraphEventHubsRuntime(provider=provider, pipeline=pipeline,
                                    checkpointer=ListCheckpointer(), client_state="secret")

    runtime.process_batch([FakeEvent(NOTIFICATION)])

    assert len(emitter.events) == 1
    ce = emitter.events[0].email
    assert ce.canonical_id == "<abc@x>"
    assert ce.subject == "Quote request"
    assert ce.from_.address == "alice@partner.com"
    assert "send a quote" in ce.body_text
```

> Verify the `MemoryEmitter` attribute name first: open `src/mailflow/emit/memory.py`.
> If captured events are exposed under a different attribute than `.events`, adjust
> the two `emitter.events` references to match (do not change the emitter).

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/adapters/graph/test_end_to_end.py -v --import-mode=importlib`
Expected: FAIL with `ImportError: cannot import name 'GraphClient' from 'mailflow.adapters.graph'`.

- [ ] **Step 3: Add curated exports**

`src/mailflow/adapters/graph/__init__.py`:
```python
"""Microsoft Graph + Azure Event Hubs adapter (app-only, no webhook)."""

from mailflow.adapters.graph.client import GraphClient
from mailflow.adapters.graph.config import EventHubConfig, GraphConfig
from mailflow.adapters.graph.extractor import GraphExtractor
from mailflow.adapters.graph.notifications import GraphNotification, parse_notification_payload
from mailflow.adapters.graph.parser import GraphEnvelopeParser
from mailflow.adapters.graph.provider import GraphProvider
from mailflow.adapters.graph.runtime import GraphEventHubsRuntime
from mailflow.adapters.graph.subscriptions import GraphSubscriptionManager

__all__ = [
    "GraphClient", "GraphConfig", "EventHubConfig", "GraphExtractor",
    "GraphNotification", "parse_notification_payload", "GraphEnvelopeParser",
    "GraphProvider", "GraphEventHubsRuntime", "GraphSubscriptionManager",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/adapters/graph/test_end_to_end.py -v --import-mode=importlib`
Expected: PASS (1 passed).

- [ ] **Step 5: Run the FULL suite + commit**

Run: `python -m pytest --import-mode=importlib`
Expected: all prior tests + the new adapter tests PASS.

```bash
git add src/mailflow/adapters/graph/__init__.py tests/adapters/graph/test_end_to_end.py
git commit -m "feat(graph): end-to-end Event Hubs -> CleanEmail flow (mocked)"
```

---

## Task 12: Live wiring stubs + dependency extras (integration-only, no unit tests)

**Files:**
- Modify: `pyproject.toml` (add optional `[graph]` extra)
- Create: `src/mailflow/adapters/graph/live.py` (real SDK glue — MSAL, httpx, azure-eventhub)
- Test: none (integration; guarded by import availability)

This isolates the only code that touches real networks/SDKs so the unit suite never imports it. It is wired at deploy time with real credentials (provisioned per `docs/graph-connection.md` §M).

- [ ] **Step 1: Add the optional extra to `pyproject.toml`**

Under `[project.optional-dependencies]`, add:
```toml
graph = ["msal>=1.28", "httpx>=0.27", "azure-eventhub>=5.11", "azure-eventhub-checkpointstoreblob-aio>=1.1"]
```

- [ ] **Step 2: Write the live glue (no unit test — imported only at deploy)**

`src/mailflow/adapters/graph/live.py`:
```python
"""Real SDK glue for the Graph + Event Hubs adapter. NOT imported by the unit
suite. Implements the adapter's internal Protocols (TokenProvider, HttpTransport)
with MSAL + httpx, and provides an azure-eventhub consume loop that drives
GraphEventHubsRuntime.process_batch. Requires the `graph` extra:
    pip install -e ".[graph]"
"""

from __future__ import annotations

from typing import Any

# These imports are intentionally local to keep the unit suite import-light.


class MsalTokenProvider:
    """App-only token via MSAL client credentials with in-memory caching."""

    def __init__(self, *, tenant_id: str, client_id: str, client_secret: str, scope: str) -> None:
        import msal  # local import

        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )
        self._scope = scope

    def get_token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=[self._scope])
        if "access_token" not in result:
            raise RuntimeError(f"token error: {result.get('error_description', result)}")
        return str(result["access_token"])


class HttpxTransport:
    """HttpTransport backed by httpx. TODO: tune timeouts/connection pooling."""

    def __init__(self) -> None:
        import httpx  # local import

        self._client = httpx.Client(timeout=30.0)

    def request(self, method: str, url: str, *, headers: dict[str, str], json: Any | None) -> Any:
        return self._client.request(method, url, headers=headers, json=json)


def run_consume_loop(*, runtime: Any, eventhub_conn: str, hub: str, consumer_group: str,
                     checkpoint_conn: str, checkpoint_container: str) -> None:
    """Blocking azure-eventhub consume loop. TODO: wire real blob checkpoint store,
    partition recovery, and graceful shutdown. Calls runtime.process_batch per batch."""
    raise NotImplementedError(
        "Integration wiring: construct EventHubConsumerClient with a "
        "BlobCheckpointStore and call runtime.process_batch on received events."
    )
```

- [ ] **Step 3: Verify the unit suite still ignores live glue**

Run: `python -m pytest --import-mode=importlib`
Expected: PASS — `live.py` is never imported by tests (no test references it).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml src/mailflow/adapters/graph/live.py
git commit -m "chore(graph): isolate live SDK glue behind optional [graph] extra"
```

---

## Self-Review

**Spec coverage** (against the SVG flow + `graph-eventhubs-delivery.md`):
- Step 1 "new email" → external; Step 2 "Graph publishes to Event Hub" → covered by subscription create with `EventHub:` URL (Task 6). Step 3 "consumer validates (skip NA, verify clientState)" → Tasks 2 + 10. Step 4 "re-fetch by id" + fallback → Tasks 4 + 9. Step 5 "pipeline parse/filter/extract/emit" → Tasks 7, 8, 11 (real `Pipeline`). ✅
- Subscription renewal + recreate-on-404 → Task 6. ✅ Lifecycle handling is adapter-internal (no core-port change), per the frozen-port decision. ✅
- `internetMessageId` dedupe → relies on the existing `DedupeStore` + `idempotency_key` exercised in the e2e `Pipeline` (Task 11). ✅
- Attachments metadata-only (streaming deferred) → Task 8/9, stated. ✅

**Placeholder scan:** no "TBD/handle edge cases" in shipped code. `live.py` `run_consume_loop` is an explicit integration stub with `NotImplementedError` (deliberate — it touches real Azure and is out of the unit scope), flagged in its own task. ✅

**Type consistency:** `GraphNotification(subscription_id, change_type, user_id, message_id)` used identically in Tasks 2, 9, 10. `GraphClient.get_message/get_message_by_internet_id/list_attachments/create_subscription/renew_subscription/delete_subscription` signatures match across Tasks 4–6, 9. `parse_notification_payload(body, *, expected_client_state)` identical in Tasks 2 and 10. `GraphExtractor.extract(msg, env)` matches the `ContentExtractor` port and the pipeline's `_extract` dispatch. ✅

**Known integration risks to verify during execution** (call out in the report, don't silently fix):
1. **mypy strict** over the new adapter tree — run `python -m mypy` after each task; apply the `CLAUDE.md` gotcha catalog (dict-splat typing, `json.loads` returns `Any` → annotate, narrow `headers.get(...)`).
2. **`MemoryEmitter` attribute** for captured events (`.events` vs other) — verified in Task 11 Step 1 note.
3. **Pipeline cursor monotonicity** — `GraphProvider` uses an incrementing per-run `order`; in push mode the Event Hub checkpoint is the real resume point, the mailflow cursor is secondary. Confirm `commit_if_ahead` is satisfied by the increasing order.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-11-mailflow-graph-eventhubs-adapter.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints for review.

**Which approach?**
