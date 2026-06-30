"""The wire contract emitted to transports (spec §14).

SCHEMA_VERSION is `major.minor`. Adding an optional field is a minor bump;
removing/retyping/re-meaning a field is a major bump. Consumers are tolerant
readers: refuse an unknown major, ignore unknown fields.
"""

from __future__ import annotations

from pydantic import BaseModel

from mailflow.core.models import CleanEmail

SCHEMA_VERSION = "1.3"


class EmailEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    tenant: str
    ordering_key: str = ""
    idempotency_key: str = ""
    email: CleanEmail
