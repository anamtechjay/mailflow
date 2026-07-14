# Graph Transport Selector (Event Hubs ⇄ Service Bus) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user pick the Graph delivery transport at the front door — `connect("graph", delivery="eventhub")` uses the existing Azure Event Hubs path, `connect("graph", delivery="servicebus")` uses the new Service Bus adapter — with identical app code otherwise.

**Architecture:** The library already has two parallel Graph ingress transports over one shared core: `graph.live.run_service` (Event Hubs) and `servicebus.live.run_service` (Service Bus, currently uncommitted under `src/mailflow/adapters/servicebus/`). Both build the SAME `GraphProvider → GraphEnvelopeParser → GraphExtractor → Pipeline`; only the consume loop + delivery config differ. This plan adds a thin **`delivery` discriminator** to `connect()` that dispatches the graph branch to the right transport builder. The selection lives entirely in the facade — no `core/`, `adapters/graph/`, or `adapters/servicebus/` code changes.

**Tech Stack:** Python 3.12, pydantic 2.6+, pytest 8+, mypy strict. Vendor SDKs (`azure-eventhub` via `[graph]`, `azure-servicebus` via `[servicebus]`) are imported locally inside each transport's `run_service`; the facade wiring imports neither. Unit tests monkeypatch `run_service`, so no SDK/network is needed.

## Global Constraints

- **Never edit `core/`, `adapters/graph/`, or `adapters/servicebus/`** — this feature is facade-only. New/changed code lives in `src/mailflow/facade.py` and `README.md`; tests in `tests/`. (CLAUDE.md ownership: `facade.py`/`connect` is the pipeline-engineer/front-door surface.)
- **No vendor SDK import in `facade.py` wiring** — the transport builders `from mailflow.adapters.<t>.live import run_service` inside the helper function body (matching the existing `_build_graph_live` at `facade.py:439`), so importing/wiring a handle needs no extra until it is actually run.
- **`delivery` is graph-only** — it selects the Graph transport; it is ignored for `provider="memory"`/`"gmail"`. Default `"eventhub"` preserves today's behavior exactly (every existing `connect("graph", …)` caller is unaffected).
- **Service Bus delivery needs both extras at runtime** — `pip install ".[servicebus,graph]"` (azure-servicebus for the queue + msal/httpx for the Graph message fetch). No `pyproject.toml` change (both extras already exist).
- **mypy strict; every commit importable.** Run `python -m pytest` and `python -m mypy` (Windows checkout — no `.venv`, use `python`).
- **This builds on the uncommitted, local-only Service Bus adapter** already in the working tree (`src/mailflow/adapters/servicebus/{config,emitter,eventgrid,runtime,composition,live}.py`). Those files must be present (not reverted) for this plan to run. **Local commits only — never `git push`.**
- **Commit trailer** on every commit: blank line then `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## CONTRACT DECISION

**CD-1 — the discriminator lives in the facade, NOT in `GraphConfig.delivery`.**
`GraphConfig` has a `delivery: Literal["eventhub", "webhook"]` field (`adapters/graph/config.py:36`), but that field describes the *adapter's* subscription notificationUrl format, a separate concern from the *front-door transport selection*. Reusing it would (a) require editing off-limits `adapters/graph/` and (b) conflate two meanings. Instead, `delivery` is a new `connect()` parameter consumed only by the facade's graph branch. `GraphConfig` is untouched. If a future need arises to persist the chosen transport on the config object, that is an additive follow-up.

---

## File Structure

- `src/mailflow/facade.py` — **modify**: add the `delivery` param to `connect()`; dispatch the graph branch; add `_graph_config()` (extracted, shared), `_servicebus_configs()`, and `_build_graph_servicebus_live()` helpers (mirroring the existing `_graph_configs`/`_build_graph_live` at `facade.py:417-471`).
- `tests/test_connect_transport_selector.py` — **create**: unit tests for the dispatch (servicebus vs eventhub vs default vs invalid) and the config mapping, all via monkeypatched `run_service` (no SDK).
- `README.md` — **modify**: document `connect("graph", delivery=…)` and the per-transport credential keys.

---

## Task 1: Facade transport builders for Service Bus delivery

**Files:**
- Modify: `src/mailflow/facade.py` (add `_graph_config`; refactor `_graph_configs` at `facade.py:417-436` to use it; add `_servicebus_configs` + `_build_graph_servicebus_live` after `_build_graph_live` at `facade.py:439-471`)
- Test: `tests/test_connect_transport_selector.py` (create)

**Interfaces:**
- Consumes: `servicebus.live.run_service(*, graph_cfg, servicebus_cfg, tenant, secret_provider, emitter, dlq_emitter, cursor_store, dedupe_store, blob_store, filters=None, cleaner=None, on_filtered="tag", connection_string=None, credential=None, ...)`; `ServiceBusConfig(fully_qualified_namespace, entity_name, connection_string_ref="")`; `GraphConfig(tenant_id, client_id, client_secret_ref, mailboxes)`; `MemoryEmitter` (already imported in facade).
- Produces:
  - `_graph_config(credentials: dict[str, Any], mailbox: str | None) -> GraphConfig` — the shared app-creds → GraphConfig builder (no SDK import).
  - `_servicebus_configs(credentials: dict[str, Any], mailbox: str | None) -> tuple[GraphConfig, ServiceBusConfig]`.
  - `_build_graph_servicebus_live(*, credentials, mailbox, tenant, emitter, secret_provider, cursor_store, dedupe_store, blob_store, filters, cleaner=None, on_filtered="tag") -> Callable[[], None]` — a blocking callable that runs the SB consume loop; parity with `_build_graph_live`.

- [ ] **Step 1: Write the failing test.** Create `tests/test_connect_transport_selector.py`:

```python
"""The connect() transport selector: delivery='servicebus' vs 'eventhub'.
All tests monkeypatch the transports' run_service, so no azure SDK / network is used."""

import pytest

from mailflow import connect

_APP = {
    "tenant_id": "t", "client_id": "c", "client_secret_ref": "env://SECRET",
    "mailboxes": ["ops@acme.com"],
}
_SB = {**_APP, "fully_qualified_namespace": "ns.servicebus.windows.net",
       "entity_name": "mailflow-graph", "connection_string": "Endpoint=sb://x"}
_EH = {**_APP, "namespace": "evh", "hub": "graph-notifications", "tenant_domain": "acme.com"}


def _spies(monkeypatch):
    calls: dict[str, dict] = {}
    monkeypatch.setattr("mailflow.adapters.servicebus.live.run_service",
                        lambda **kw: calls.__setitem__("sb", kw))
    monkeypatch.setattr("mailflow.adapters.graph.live.run_service",
                        lambda **kw: calls.__setitem__("eh", kw))
    return calls


def test_delivery_servicebus_dispatches_to_service_bus(monkeypatch):
    calls = _spies(monkeypatch)
    mf = connect("graph", delivery="servicebus", credentials=_SB, tenant="acme")
    mf.run()                                   # invokes the live closure -> patched run_service
    assert "sb" in calls and "eh" not in calls
    assert calls["sb"]["servicebus_cfg"].entity_name == "mailflow-graph"
    assert calls["sb"]["servicebus_cfg"].fully_qualified_namespace == "ns.servicebus.windows.net"
    assert calls["sb"]["graph_cfg"].client_id == "c"
    assert calls["sb"]["tenant"] == "acme"
    assert calls["sb"]["connection_string"] == "Endpoint=sb://x"


def test_default_delivery_is_eventhub(monkeypatch):
    calls = _spies(monkeypatch)
    mf = connect("graph", credentials=_EH, tenant="acme")   # no delivery= -> eventhub
    mf.run()
    assert "eh" in calls and "sb" not in calls
    assert calls["eh"]["eventhub"].hub == "graph-notifications"


def test_explicit_delivery_eventhub(monkeypatch):
    calls = _spies(monkeypatch)
    connect("graph", delivery="eventhub", credentials=_EH, tenant="acme").run()
    assert "eh" in calls and "sb" not in calls


def test_invalid_delivery_raises_value_error():
    with pytest.raises(ValueError, match="delivery"):
        connect("graph", delivery="kafka", credentials=_SB)   # type: ignore[arg-type]


def test_servicebus_delivery_threads_on_filtered(monkeypatch):
    calls = _spies(monkeypatch)
    connect("graph", delivery="servicebus", credentials=_SB, tenant="acme",
            on_filtered="drop").run()
    assert calls["sb"]["on_filtered"] == "drop"
```

  > Why `mf.run()` works without SDKs: `_build_graph_servicebus_live` does `from mailflow.adapters.servicebus.live import run_service` at BUILD time (during `connect()`), binding to whatever the module attribute is then — which the monkeypatch has already replaced. `servicebus.live`'s azure imports are all function-local, so importing the module pulls in no SDK. `mf.run()` calls the closure, which calls the patched no-op.

- [ ] **Step 2: Run it to verify it fails.** Run: `python -m pytest tests/test_connect_transport_selector.py -v`
  Expected: FAIL — `connect()` has no `delivery` keyword (TypeError), and `_build_graph_servicebus_live` does not exist. (Task 1 adds the builders; Task 2 adds the `delivery` param + dispatch. Expect these tests to stay red until Task 2 — that is fine; this test file is the acceptance target for the pair. If you prefer a green Task 1, temporarily test `_build_graph_servicebus_live` directly, then let the full file go green after Task 2.)

- [ ] **Step 3: Add the shared `_graph_config` and refactor `_graph_configs`.** In `src/mailflow/facade.py`, replace the existing `_graph_configs` (currently at `facade.py:417-436`) with:

```python
def _graph_config(credentials: dict[str, Any], mailbox: str | None) -> Any:
    """Build just the GraphConfig (transport-agnostic app credentials). Shared by the
    Event Hubs and Service Bus delivery paths. No SDK import — pydantic config only."""
    from mailflow.adapters.graph.config import GraphConfig

    mailboxes = [mailbox] if mailbox else list(credentials.get("mailboxes", []))
    return GraphConfig(
        tenant_id=str(credentials["tenant_id"]),
        client_id=str(credentials["client_id"]),
        client_secret_ref=str(credentials["client_secret_ref"]),
        mailboxes=mailboxes,
    )


def _graph_configs(credentials: dict[str, Any], mailbox: str | None) -> tuple[Any, Any]:
    """Build (GraphConfig, EventHubConfig) from the credentials dict (spec §graph). The
    Azure/MSAL SDKs are NOT imported here — only pydantic config objects are built, so
    wiring a Graph handle needs no `graph` extra until it is actually run."""
    from mailflow.adapters.graph.config import EventHubConfig

    graph_cfg = _graph_config(credentials, mailbox)
    eventhub = EventHubConfig(
        namespace=str(credentials["namespace"]),
        hub=str(credentials["hub"]),
        tenant_domain=str(credentials.get("tenant_domain", "")),
        consumer_group=str(credentials.get("consumer_group", "$Default")),
    )
    return graph_cfg, eventhub
```

- [ ] **Step 4: Add `_servicebus_configs` and `_build_graph_servicebus_live`.** Insert immediately after `_build_graph_live` (which ends at `facade.py:471`):

```python
def _servicebus_configs(credentials: dict[str, Any], mailbox: str | None) -> tuple[Any, Any]:
    """Build (GraphConfig, ServiceBusConfig) from the credentials dict for the Service Bus
    delivery path. No SDK import — pydantic configs only. The SB connection is passed to
    run_service as `connection_string` (like the Event Hubs path), so `connection_string_ref`
    stays optional here."""
    from mailflow.adapters.servicebus.config import ServiceBusConfig

    graph_cfg = _graph_config(credentials, mailbox)
    servicebus = ServiceBusConfig(
        fully_qualified_namespace=str(credentials["fully_qualified_namespace"]),
        entity_name=str(credentials["entity_name"]),
        connection_string_ref=str(credentials.get("connection_string_ref", "")),
    )
    return graph_cfg, servicebus


def _build_graph_servicebus_live(
    *,
    credentials: dict[str, Any],
    mailbox: str | None,
    tenant: str,
    emitter: Emitter,
    secret_provider: Any,
    cursor_store: CursorStore,
    dedupe_store: Any,
    blob_store: Any,
    filters: list[Filter],
    cleaner: Any = None,
    on_filtered: Literal["tag", "drop"] = "tag",
) -> Callable[[], None]:
    """Return a blocking callable that runs the live Graph → Event Grid → Service Bus consume
    loop — parity with `_build_graph_live` (Event Hubs). The azure/msal SDKs import lazily
    inside servicebus.live.run_service, so importing/wiring this needs no extra."""
    from mailflow.adapters.servicebus.live import run_service

    graph_cfg, servicebus_cfg = _servicebus_configs(credentials, mailbox)

    def live() -> None:
        run_service(
            graph_cfg=graph_cfg, servicebus_cfg=servicebus_cfg, tenant=tenant,
            secret_provider=secret_provider, emitter=emitter, dlq_emitter=MemoryEmitter(),
            cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
            filters=filters, cleaner=cleaner, on_filtered=on_filtered,
            connection_string=credentials.get("connection_string"),
            credential=credentials.get("credential"),
        )

    return live
```

- [ ] **Step 5: Run mypy + the (still-red-until-Task-2) test.** Run: `python -m mypy` — Expected: clean (the new helpers type-check; `Callable`, `Emitter`, `CursorStore`, `Filter`, `Literal`, `Any` are already imported in `facade.py`). Run: `python -m pytest tests/test_connect_transport_selector.py -v` — Expected: still FAIL on the `delivery` kwarg (added in Task 2). Run the existing graph connect tests to confirm the `_graph_configs` refactor didn't regress: `python -m pytest -k "graph and connect" -v` (and the full suite `python -m pytest`) — Expected: all previously-passing tests still pass.

- [ ] **Step 6: Commit.**

```bash
git add src/mailflow/facade.py tests/test_connect_transport_selector.py
git commit -m "feat(facade): Service Bus delivery builders (_build_graph_servicebus_live) + shared _graph_config

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Wire `delivery=` into `connect()` and dispatch the graph branch

**Files:**
- Modify: `src/mailflow/facade.py` (add `delivery` param to `connect()` at `facade.py:216-234`; dispatch in the `provider == "graph"` branch at `facade.py:303-318`)
- Test: `tests/test_connect_transport_selector.py` (from Task 1 — now goes green)

**Interfaces:**
- Consumes: `_build_graph_servicebus_live` and `_build_graph_live` (Task 1 / existing).
- Produces: `connect(provider, *, …, delivery: Literal["eventhub", "servicebus"] = "eventhub") -> Mailflow`. `delivery` is consulted only in the graph branch.

- [ ] **Step 1: Confirm the target test is red for the right reason.** Run: `python -m pytest tests/test_connect_transport_selector.py -v` — Expected: FAIL with `TypeError: connect() got an unexpected keyword argument 'delivery'`.

- [ ] **Step 2: Add the `delivery` parameter.** In `src/mailflow/facade.py`, the `connect(` signature currently ends (at `facade.py:233`) with `on_filtered: Literal["tag", "drop"] = "tag",`. Add one line directly after it, before the closing `) -> Mailflow:`:

```python
    on_filtered: Literal["tag", "drop"] = "tag",
    delivery: Literal["eventhub", "servicebus"] = "eventhub",
) -> Mailflow:
```

  Also update the `connect` docstring's provider line (at `facade.py:236`) to mention delivery. Change:

```python
    """Wire a runnable Mailflow for the given provider. `provider` is
    "memory" | "gmail" | "graph". `filters` is the unified list (spec §3);
```
  to:
```python
    """Wire a runnable Mailflow for the given provider. `provider` is
    "memory" | "gmail" | "graph". For "graph", `delivery` selects the transport:
    "eventhub" (default, Azure Event Hubs) or "servicebus" (Event Grid → Service Bus).
    `filters` is the unified list (spec §3);
```

- [ ] **Step 3: Dispatch the graph branch.** Replace the existing graph branch (`facade.py:303-318`):

```python
    if provider == "graph":
        live = _build_graph_live(
            credentials=credentials or {}, mailbox=mailbox, tenant=tenant,
            emitter=pipe_emitter, secret_provider=secret_provider or EnvSecretProvider(),
            cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
            filters=chain, cleaner=cleaner,
        )
        fetcher = _build_graph_fetcher(
            credentials=credentials or {}, mailbox=mailbox,
            secret_provider=secret_provider or EnvSecretProvider(), cleaner=cleaner,
        )
        return Mailflow(
            provider_kind="graph", emitter=emitter, cursor_store=cursor_store,
            live_run=live, queue=queue, fetcher=fetcher, project=project,
            dedupe_store=dedupe_store, blob_store=blob_store,
        )
```

  with the transport-selecting version:

```python
    if provider == "graph":
        if delivery not in ("eventhub", "servicebus"):
            raise ValueError(
                f"unknown delivery {delivery!r} for provider 'graph' "
                "(use 'eventhub' or 'servicebus')"
            )
        if delivery == "servicebus":
            live = _build_graph_servicebus_live(
                credentials=credentials or {}, mailbox=mailbox, tenant=tenant,
                emitter=pipe_emitter, secret_provider=secret_provider or EnvSecretProvider(),
                cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
                filters=chain, cleaner=cleaner, on_filtered=on_filtered,
            )
        else:  # "eventhub" (default) — unchanged behavior
            live = _build_graph_live(
                credentials=credentials or {}, mailbox=mailbox, tenant=tenant,
                emitter=pipe_emitter, secret_provider=secret_provider or EnvSecretProvider(),
                cursor_store=cursor_store, dedupe_store=dedupe_store, blob_store=blob_store,
                filters=chain, cleaner=cleaner,
            )
        fetcher = _build_graph_fetcher(          # transport-agnostic (direct Graph REST fetch)
            credentials=credentials or {}, mailbox=mailbox,
            secret_provider=secret_provider or EnvSecretProvider(), cleaner=cleaner,
        )
        return Mailflow(
            provider_kind="graph", emitter=emitter, cursor_store=cursor_store,
            live_run=live, queue=queue, fetcher=fetcher, project=project,
            dedupe_store=dedupe_store, blob_store=blob_store,
        )
```

  > Note: the Event Hubs `_build_graph_live` does not accept `on_filtered` (its `run_service` doesn't take it — a pre-existing gap in the EH path). Only the Service Bus builder threads `on_filtered`. Do NOT add `on_filtered=` to the `_build_graph_live` call.

- [ ] **Step 4: Run the target tests to verify they pass.** Run: `python -m pytest tests/test_connect_transport_selector.py -v` — Expected: all 5 PASS.

- [ ] **Step 5: Run the full suite + mypy.** Run: `python -m pytest` — Expected: all pass (no regression; the invalid-delivery ValueError path and the two transport paths are covered). Run: `python -m mypy` — Expected: clean.

- [ ] **Step 6: Commit.**

```bash
git add src/mailflow/facade.py tests/test_connect_transport_selector.py
git commit -m "feat(connect): add delivery=eventhub|servicebus selector for the graph provider

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Document the transport selector in the README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a short "Choosing the Graph transport" subsection.** In the "Live: Outlook via Event Grid → Azure Service Bus" section (added by the Service Bus plan), add a subsection that shows the one-line switch and the per-transport credential keys. Use this content verbatim:

````markdown
### Choosing the Graph transport: `delivery=`

Same Graph mail, your choice of transport — only one argument changes:

```python
# Event Hubs (default) — needs the [graph] extra
mf = connect("graph", delivery="eventhub", credentials={
    "tenant_id": "<tenant-guid>", "client_id": "<app-guid>",
    "client_secret_ref": "env://GRAPH_CLIENT_SECRET", "mailboxes": ["ops@acme.com"],
    "namespace": "evh-mailflow", "hub": "graph-notifications", "tenant_domain": "acme.com",
    "connection_string": "<eventhub-connection-string>",   # or omit + pass credential=
})

# Service Bus (Event Grid Partner Topic → Service Bus) — needs the [servicebus,graph] extras
mf = connect("graph", delivery="servicebus", credentials={
    "tenant_id": "<tenant-guid>", "client_id": "<app-guid>",
    "client_secret_ref": "env://GRAPH_CLIENT_SECRET", "mailboxes": ["ops@acme.com"],
    "fully_qualified_namespace": "<ns>.servicebus.windows.net", "entity_name": "mailflow-graph",
    "connection_string": "<servicebus-connection-string>",  # or omit + pass credential=
})

for email in mf.stream():   # identical downstream, whichever transport you chose
    my_app.save(email)
```

`delivery` defaults to `"eventhub"`, so existing `connect("graph", …)` code is unchanged. It
applies only to the `"graph"` provider. Install the matching extra:
`pip install -e ".[graph]"` for Event Hubs, `pip install -e ".[servicebus,graph]"` for Service
Bus. Provisioning for the Service Bus path is in `docs/azure-servicebus-setup.md`.
````

- [ ] **Step 2: Commit.**

```bash
git add README.md
git commit -m "docs: document connect(delivery=eventhub|servicebus) transport selector

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review (completed by plan author)

1. **Spec coverage:** `delivery="eventhub"` → existing EH path ✓ (Task 2 `else` branch, unchanged builder); `delivery="servicebus"` → new SB path ✓ (Task 1 builder + Task 2 dispatch); default preserves behavior ✓ (default `"eventhub"`, EH builder call unchanged); no core/graph/servicebus edits ✓ (facade-only, CD-1); extras per transport documented ✓ (Task 3); TDD ✓ (test file drives Tasks 1–2); README ✓ (Task 3).
2. **Placeholder scan:** every code step contains complete code; the one deliberate cross-task redness (Task 1's test stays red until Task 2) is called out explicitly with the reason and a green-Task-1 alternative — not a TODO.
3. **Type consistency:** `_graph_config(credentials, mailbox) -> GraphConfig` used identically in `_graph_configs` and `_servicebus_configs`; `_build_graph_servicebus_live(**kwargs)` keyword set matches the Task 2 dispatch call exactly (`credentials, mailbox, tenant, emitter, secret_provider, cursor_store, dedupe_store, blob_store, filters, cleaner, on_filtered`); `servicebus.live.run_service` kwargs (`graph_cfg, servicebus_cfg, tenant, secret_provider, emitter, dlq_emitter, cursor_store, dedupe_store, blob_store, filters, cleaner, on_filtered, connection_string, credential`) match the real signature (`adapters/servicebus/live.py`); `ServiceBusConfig(fully_qualified_namespace, entity_name, connection_string_ref)` matches the real model; `delivery: Literal["eventhub", "servicebus"]` spelled identically in the signature, dispatch, and tests.

**Verification points flagged inline:** (a) the `_graph_configs` refactor must not regress the existing Event Hubs connect path — run `-k "graph and connect"`; (b) the monkeypatch-before-`connect()` ordering in the tests is load-bearing (documented in Task 1 Step 1's note); (c) do NOT pass `on_filtered=` to `_build_graph_live` (EH `run_service` rejects it).
