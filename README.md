# mailflow (core spine)

Provider- and transport-agnostic email ingestion. This package is the **core**:
the full pipeline (parse → filter → extract → emit) plus the §8 correctness
invariants, runnable end-to-end against an in-memory provider with zero setup.

## Quickstart (zero external setup)

```python
from mailflow import build_from_config, MailflowConfig
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

cfg = MailflowConfig.model_validate({
    "tenant": "acme",
    "provider": {"kind": "memory"},
    "filters": [{"kind": "blacklist", "params": {"domains": ["spam.com"]}}],
    "emitter": {"kind": "memory"},
})
stream = StreamRef(mailbox="ops@acme.com", folder="Inbox")
seed = {stream: [SeedEmail("m1", b"Message-ID: <m1@x>\r\nFrom: a@partner.com\r\nSubject: hi\r\n\r\nhello")]}

pipe = build_from_config(cfg, seed=seed)
report = pipe.run_once()
print(report.emitted, report.dropped, report.dead_lettered)
```

## What's here vs later

| Plan | Scope |
|------|-------|
| **1 (this repo)** | core + in-memory adapters |
| 2 | Microsoft Graph adapter + webhook/subscribe |
| 3 | Gmail adapter |
| 4 | reference deploy, allowlisted plugins, attachment streaming, LLM classifier |

The wire contract is `EmailEvent` / `CleanEmail` at `SCHEMA_VERSION = "1.0"`.
Adapters plug into the ports in `mailflow.core.ports` without touching `core`.
