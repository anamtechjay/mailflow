# Attachment Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Make mailflow's attachment handling real for the V1 fast-follow: (1) true chunked attachment streaming with a hard per-attachment byte cap that **fails closed** (over-cap / undecodable attachment → DLQ, never fully buffered, never persisted); (2) an `AttachmentScanner` safety **seam** (no-op default + content-type/extension allowlist) invoked **before** any blob is persisted; (3) **explicit, tested** content-addressed dedupe so the same attachment in two messages is stored exactly once.

**Architecture:** Extraction stays in `extract/mime.py` (`MimeExtractor.extract_bytes`). Two new pure modules carry the new behaviour: `extract/streaming.py` (chunked decode + cap) and `extract/safety.py` (scanner default + allowlist). The cap/scan failures raise `PermanentError` subclasses so the **existing** `Pipeline._process` `except PermanentError → _dead_letter` path dead-letters the message — honouring the frozen DLQ-counting contract (`add_dead_letter()` + paired `record()` as one unit). This deliberately parallels the **B1** message-level size guard in `core/pipeline.py` (`_process`, the `size <= 0` / `size > max_message_bytes` fail-closed branches, lines 113–126). Dedupe is enforced inside each `BlobStore.put_stream` (skip rewrite when the content-addressed `ref` already exists) — no port change.

**Tech Stack:** Python 3, pydantic 2.13, stdlib `email`/`base64`/`quopri`/`hashlib`, pytest 9, mypy --strict. Run tests/types with the venv interpreter:
`/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest`
`/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
mypy is configured `packages = ["mailflow"]`, strict — **every commit must leave the tree importable** (commit a module before the modules that import it). Every commit message ends with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. **Never `git push`.**

---

## Ownership / coordination note

If executed by the multi-agent team rather than a single worker, file ownership crosses two roles:
- `extract/*`, `stores/*` → **extract-filter-engineer** / **adapters-engineer**.
- `core/models.py`, `core/ports.py` → **core-foundation-engineer** (Task 2, Step 1).

Per CLAUDE.md, do not patch another teammate's file silently — the core-model/port additions in Task 2 Step 1 must be done by (or handed to) core-foundation-engineer. A single solo worker owns all of it and can proceed top-to-bottom.

## Frozen contracts honoured (do not relitigate)
- **DLQ counting:** the new fail-closed errors are `PermanentError` subclasses; the pipeline's existing `_dead_letter` is the *only* path that counts them (`add_dead_letter()` + paired `record(_trace(...dead_lettered...))`). We add **no** new `dead_lettered` trace anywhere. One poison/over-cap/blocked attachment ⇒ exactly one DLQ count.
- **Fail-closed posture:** mirrors B1 — an attachment we cannot vouch is within budget (over-cap) or cannot decode (unknown size) is rejected, never persisted.
- **BlobStore port unchanged:** `put_stream(ref, chunks, content_type) -> str` / `open(ref) -> Iterator[bytes]` stay identical (A11 stability). Dedupe lives *inside* the two impls.

## What we are NOT doing
- No real AV/CDR scanner (P2). The `AttachmentScanner` slot ships with a no-op default only.
- No new config-schema knobs / builder wiring: `MimeExtractor()` with all-default args keeps current behaviour, so `builder.py`, `facade.py`, `registry.py`, and the Gmail composition root need **no** edits. Non-default caps/allowlists are injected by constructing `MimeExtractor(...)` (exercised in tests).
- No change to the `Attachment` model fields, `CleanEmail`, or the extractor seam in `Pipeline._extract`.

---

## Task 1 — True chunked streaming + fail-closed per-attachment byte cap

Replace the fake `iter([payload])` (whole payload buffered, no cap) with a real chunked decoder and a hard cap that fails closed.

**Files:**
- **Create:** `src/mailflow/extract/streaming.py`
- **Create (test):** `tests/extract/test_attachment_streaming.py`
- **Create (test):** `tests/test_pipeline_attachment_failclosed.py`
- **Modify:** `src/mailflow/extract/mime.py` — add `__init__` (before `extract_bytes`, currently line 40); rewrite the attachment branch of `_walk_body` (currently lines 122–172, specifically the per-part `decoded`/`payload` lines 136–137 and the `if is_attachment or is_inline_media:` block lines 149–166); drop `import hashlib` (line 6, no longer used).

### Step 1: Write failing tests for the streaming module

- [ ] **Step 1: Failing tests for `streaming.py`.** Create `tests/extract/test_attachment_streaming.py`:

```python
"""Attachment streaming + fail-closed per-attachment byte cap (V1 fast-follow)."""

from __future__ import annotations

import hashlib
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy

import pytest

from mailflow.extract.streaming import (
    AttachmentTooLargeError,
    AttachmentUnreadableError,
    digest_and_size,
    iter_decoded,
)


def _attachment_part(
    data: bytes, *, filename: str = "report.pdf", ctype: str = "application/pdf"
) -> EmailMessage:
    m = EmailMessage()
    m["Message-ID"] = "<a.1@example.com>"
    m["From"] = "alice@partner.com"
    m["To"] = "ops@acme.com"
    m["Subject"] = "with attachment"
    m.set_content("see attached")
    maintype, subtype = ctype.split("/", 1)
    m.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    parsed = message_from_bytes(m.as_bytes(), policy=default_policy)
    assert isinstance(parsed, EmailMessage)
    return next(p for p in parsed.walk() if p.get_filename() == filename)


def test_iter_decoded_streams_in_chunks_and_round_trips() -> None:
    data = bytes(range(256)) * 40  # 10 KiB, base64-encoded by add_attachment
    part = _attachment_part(data)
    chunks = list(iter_decoded(part, chunk_size=1024))
    assert len(chunks) > 1  # genuinely chunked, not one buffer
    assert b"".join(chunks) == data  # lossless round-trip


def test_digest_and_size_matches_sha256_and_length() -> None:
    data = b"hello attachment world" * 100
    part = _attachment_part(data)
    digest, size = digest_and_size(part, cap=10_000_000)
    assert size == len(data)
    assert digest == hashlib.sha256(data).hexdigest()


def test_over_cap_attachment_fails_closed() -> None:
    part = _attachment_part(b"x" * 5000)
    with pytest.raises(AttachmentTooLargeError):
        digest_and_size(part, cap=1024)


def test_invalid_base64_is_unreadable_fail_closed() -> None:
    raw = (
        b"Message-ID: <bad.1@example.com>\r\n"
        b"From: alice@partner.com\r\n"
        b"To: ops@acme.com\r\n"
        b"Subject: bad\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="x.bin"\r\n'
        b"Content-Transfer-Encoding: base64\r\n"
        b"\r\n"
        b"A\r\n"  # 1 base64 data char -> binascii.Error (invalid length)
    )
    parsed = message_from_bytes(raw, policy=default_policy)
    assert isinstance(parsed, EmailMessage)
    part = next(p for p in parsed.walk() if p.get_filename() == "x.bin")
    with pytest.raises(AttachmentUnreadableError):
        digest_and_size(part, cap=10_000_000)
```

- [ ] **Step 2: Run the test, confirm it fails for the right reason.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/extract/test_attachment_streaming.py -q`
  Expected: collection/import error — `ModuleNotFoundError: No module named 'mailflow.extract.streaming'` (the module does not exist yet).

- [ ] **Step 3: Implement `src/mailflow/extract/streaming.py` (minimal).**

```python
"""Chunked, fail-closed attachment streaming (V1 fast-follow).

The legacy path decoded a whole attachment into memory then wrapped it in
`iter([payload])` — no cap, fully buffered. This module decodes a leaf MIME part
incrementally and enforces a hard per-attachment byte cap that fails CLOSED:
an over-cap or undecodable attachment raises a PermanentError, so the pipeline
dead-letters the message (parallels the B1 message-level size guard) — it is
never fully buffered nor persisted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import quopri
from email.message import EmailMessage
from typing import Iterator

from mailflow.core.errors import PermanentError

# Tighter than B1's 50 MB *message* ceiling (PipelineConfig.max_message_bytes).
MAX_ATTACHMENT_BYTES = 25_000_000  # 25 MB per attachment


class AttachmentTooLargeError(PermanentError):
    """A single attachment exceeded the per-attachment byte cap (fail-closed -> DLQ)."""

    def __init__(self, cap: int, seen: int) -> None:
        self.cap = cap
        self.seen = seen
        super().__init__(f"attachment exceeds the {cap}-byte cap (saw > {seen} bytes)")


class AttachmentUnreadableError(PermanentError):
    """An attachment's bytes could not be decoded (unknown size -> fail-closed -> DLQ)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"attachment unreadable (fail-closed): {reason}")


def iter_decoded(part: EmailMessage, chunk_size: int = 65536) -> Iterator[bytes]:
    """Yield a leaf part's *decoded* bytes in chunks of ~`chunk_size`, without
    materialising the whole decoded payload at once. Supports base64 /
    quoted-printable / 7bit / 8bit / binary transfer encodings."""
    cte = (part.get("content-transfer-encoding") or "7bit").strip().lower()
    raw = part.get_payload(decode=False)
    if not isinstance(raw, str):
        raise AttachmentUnreadableError(f"payload is {type(raw).__name__}, not text")
    if cte == "base64":
        data = "".join(raw.split())  # drop the 76-col line folding / whitespace
        step = (chunk_size // 3 + 1) * 4  # decode on 4-char -> 3-byte quanta boundaries
        for i in range(0, len(data), step):
            try:
                yield base64.b64decode(data[i : i + step])
            except binascii.Error as exc:
                raise AttachmentUnreadableError(f"invalid base64: {exc}") from exc
    elif cte == "quoted-printable":
        # Decoded QP is never larger than its encoded form -> fail closed pre-decode.
        encoded = raw.encode("latin-1", "surrogateescape")
        yield quopri.decodestring(encoded)
    else:  # 7bit / 8bit / binary / identity
        for i in range(0, len(raw), chunk_size):
            yield raw[i : i + chunk_size].encode("latin-1", "surrogateescape")


def digest_and_size(part: EmailMessage, *, cap: int, chunk_size: int = 65536) -> tuple[str, int]:
    """Stream the part once to compute (sha256_hex, size_bytes), enforcing the cap
    fail-closed. Nothing is buffered beyond a single chunk; raises before the running
    count can exceed `cap`. Returns ("", 0) for a genuinely empty part (preserving the
    legacy `content_hash = "" if not payload` behaviour)."""
    hasher = hashlib.sha256()
    total = 0
    for chunk in iter_decoded(part, chunk_size):
        total += len(chunk)
        if total > cap:
            raise AttachmentTooLargeError(cap, total)
        hasher.update(chunk)
    return (hasher.hexdigest(), total) if total else ("", 0)
```

- [ ] **Step 4: Run green + mypy.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/extract/test_attachment_streaming.py -q` → all pass.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` → `Success: no issues found`.

- [ ] **Step 5: Commit.**
```
git add src/mailflow/extract/streaming.py tests/extract/test_attachment_streaming.py
git commit -m "$(cat <<'EOF'
feat(attach): chunked attachment decode + fail-closed per-attachment byte cap

iter_decoded streams a leaf MIME part in bounded chunks (base64/QP/identity);
digest_and_size enforces MAX_ATTACHMENT_BYTES fail-closed, raising
AttachmentTooLargeError / AttachmentUnreadableError (both PermanentError) so the
pipeline's existing DLQ path handles them. Parallels the B1 message-size guard.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 6: Wire the cap into the extractor (fail-closed end to end)

- [ ] **Step 6: Failing pipeline test for the cap.** Create `tests/test_pipeline_attachment_failclosed.py`:

```python
"""Per-attachment cap fails closed at the pipeline boundary: an over-cap attachment
dead-letters the whole message exactly once (parallels the B1 message-size guard)."""

from __future__ import annotations

from email.message import EmailMessage

from mailflow.core.models import StreamRef
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.extract.envelope import MimeEnvelopeParser
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain
from mailflow.providers.memory import MemoryProvider, SeedEmail
from mailflow.stores.memory import (
    InMemoryBlobStore,
    InMemoryCursorStore,
    InMemoryDedupeStore,
)

STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _raw_with_big_attachment() -> bytes:
    m = EmailMessage()
    m["Message-ID"] = "<big.1@example.com>"
    m["From"] = "alice@partner.com"
    m["To"] = "ops@acme.com"
    m["Subject"] = "huge"
    m.set_content("see attached")
    m.add_attachment(b"A" * 4096, maintype="application", subtype="pdf", filename="big.pdf")
    return m.as_bytes()


def _pipe(extractor: MimeExtractor) -> tuple[Pipeline, MemoryEmitter, MemoryEmitter]:
    emit, dlq = MemoryEmitter(), MemoryEmitter()
    raw = _raw_with_big_attachment()
    pipe = Pipeline(
        provider=MemoryProvider(seed={STREAM: [SeedEmail("m1", raw)]}),
        parser=MimeEnvelopeParser(),
        filters=FilterChain([]),
        extractor=extractor,
        emitter=emit,
        dlq_emitter=dlq,
        cursor_store=InMemoryCursorStore(),
        dedupe_store=InMemoryDedupeStore(),
        blob_store=InMemoryBlobStore(),
        config=PipelineConfig(tenant="acme"),
    )
    return pipe, emit, dlq


def test_over_cap_attachment_dead_letters_once() -> None:
    pipe, emit, dlq = _pipe(MimeExtractor(max_attachment_bytes=64))
    r = pipe.run_once()
    assert r.emitted == 0 and len(emit.events) == 0
    assert r.dead_lettered == 1 and len(dlq.events) == 1


def test_under_cap_attachment_still_emits() -> None:
    pipe, emit, dlq = _pipe(MimeExtractor(max_attachment_bytes=1_000_000))
    r = pipe.run_once()
    assert r.emitted == 1 and r.dead_lettered == 0 and len(emit.events) == 1
```

- [ ] **Step 7: Run, confirm it fails for the right reason.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_pipeline_attachment_failclosed.py -q`
  Expected: `test_over_cap_attachment_dead_letters_once` fails — `MimeExtractor(max_attachment_bytes=...)` raises `TypeError: __init__() got an unexpected keyword argument 'max_attachment_bytes'` (no `__init__` yet; the cap is not wired).

- [ ] **Step 8: Add the cap to `MimeExtractor` and stream in `_walk_body`.** In `src/mailflow/extract/mime.py`:

  (a) Delete the now-unused `import hashlib` (line 6).

  (b) Update the imports for streaming + the `MAX_ATTACHMENT_BYTES` cap. Replace the existing model/port imports (lines 15–16) with:
```python
from mailflow.core.models import Attachment, CleanEmail, Direction, Recipient
from mailflow.core.ports import BlobStore
from mailflow.extract.clean import html_to_text, normalize_subject
from mailflow.extract.streaming import (
    MAX_ATTACHMENT_BYTES,
    digest_and_size,
    iter_decoded,
)
```
  (Keep the existing `from mailflow.extract.clean import ...` line — shown here for placement; do not duplicate it.)

  (c) Add an `__init__` immediately above `extract_bytes` (current line 40), inside `class MimeExtractor`:
```python
    def __init__(self, *, max_attachment_bytes: int = MAX_ATTACHMENT_BYTES) -> None:
        self.max_attachment_bytes = max_attachment_bytes
```
  (d) In `_walk_body`, **remove** the two per-part decode lines (current 136–137):
```python
            decoded = part.get_payload(decode=True)
            payload: bytes = decoded if isinstance(decoded, bytes) else b""
```
  and **replace** the attachment block (current lines 149–166) with the streaming version:
```python
            if is_attachment or is_inline_media:
                content_hash, size_bytes = digest_and_size(
                    part, cap=self.max_attachment_bytes
                )
                storage_ref = ""
                # Stream the bytes into the blob store (chunked, not buffered) so
                # downstream apps can download the file (else metadata only).
                if blob_store is not None and size_bytes:
                    storage_ref = blob_store.put_stream(
                        content_hash, iter_decoded(part), ctype
                    )
                attachments.append(
                    Attachment(
                        filename=filename or "",
                        content_type=ctype,
                        size_bytes=size_bytes,
                        content_hash=content_hash,
                        content_id=str(cid) if cid else "",
                        is_inline=bool(is_inline_media and not is_attachment),
                        storage_ref=storage_ref,
                    )
                )
```

- [ ] **Step 9: Run green (new + existing mime/pipeline suites) + mypy.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/test_pipeline_attachment_failclosed.py tests/extract/test_mime.py tests/test_pipeline_size_failclosed.py -q` → all pass.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` → `Success`.

- [ ] **Step 10: Commit.**
```
git add src/mailflow/extract/mime.py tests/test_pipeline_attachment_failclosed.py
git commit -m "$(cat <<'EOF'
feat(attach): stream attachments via the cap guard; over-cap -> DLQ

MimeExtractor now decodes each attachment part in chunks (iter_decoded) and
enforces a per-attachment cap (digest_and_size). An over-cap/undecodable
attachment raises PermanentError, dead-lettering the message exactly once
through the pipeline's existing _dead_letter path. Removes the hashlib import
and the whole-payload buffering / iter([payload]) seam.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — Attachment safety seam (allowlist + scanner hook, no-op default)

Add the safety **slot**: a `ScanResult`/`ScanVerdict` model, an `AttachmentScanner` port, a no-op default impl, and a content-type/extension allowlist — invoked **before** the blob is persisted. Default behaviour is unchanged (no-op scanner + empty allowlist = allow all).

**Files:**
- **Modify:** `src/mailflow/core/models.py` — add `ScanVerdict` enum + `ScanResult` model (after the `Verdict` enum, current line 39).
- **Modify:** `src/mailflow/core/ports.py` — add `Attachment`, `ScanResult` to the models import (lines 14–22); add the `AttachmentScanner` Protocol (after `TokenRotationSink`, current line 140).
- **Create:** `src/mailflow/extract/safety.py`
- **Modify:** `src/mailflow/extract/mime.py` — extend `__init__` with `scanner`/`allowlist`; insert the safety check before persistence in `_walk_body`.
- **Create (test):** `tests/extract/test_attachment_safety.py`

### Step 1: Add the core model + port (committed first — pipeline/extract import them)

- [ ] **Step 1: Add `ScanVerdict` + `ScanResult` to `core/models.py`.** Insert immediately after the `Verdict` enum (current line 39, before `class Recipient`):
```python
class ScanVerdict(str, Enum):
    """Outcome of an attachment safety scan (V1 fast-follow)."""

    allow = "allow"
    block = "block"


class ScanResult(BaseModel):
    """An AttachmentScanner / allowlist verdict. Allow by default (no-op posture)."""

    verdict: ScanVerdict = ScanVerdict.allow
    reason: str = ""
```

- [ ] **Step 2: Add the `AttachmentScanner` port to `core/ports.py`.** Extend the models import (lines 14–22) to include `Attachment` and `ScanResult`:
```python
from mailflow.core.models import (
    Attachment,
    CleanEmail,
    Cursor,
    Envelope,
    RawMessage,
    Relevance,
    ScanResult,
    StreamRef,
    WebhookIdentity,
)
```
  Then append after the `TokenRotationSink` port (current line 140):
```python
@provisional
@runtime_checkable
class AttachmentScanner(Protocol):
    """Safety hook run on each attachment BEFORE its bytes are persisted. The V1
    default is a no-op (allow-all); a real AV/CDR scanner is a P2 concern."""

    def scan(self, attachment: Attachment) -> ScanResult: ...
```

- [ ] **Step 3: Verify the tree still imports + mypy clean (no test yet — this is a pure additive surface).**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q` → existing suite still green (nothing references the new names yet).
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` → `Success`.

- [ ] **Step 4: Commit.**
```
git add src/mailflow/core/models.py src/mailflow/core/ports.py
git commit -m "$(cat <<'EOF'
feat(attach): ScanResult/ScanVerdict model + AttachmentScanner port (slot)

Adds the provisional safety seam surface: a verdict model and a runtime_checkable
AttachmentScanner Protocol (scan(attachment) -> ScanResult). No-op default and
allowlist land next; the real scanner is a P2 concern.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 5: Safety module + extractor wiring (TDD)

- [ ] **Step 5: Failing tests for the safety seam.** Create `tests/extract/test_attachment_safety.py`:

```python
"""Attachment safety seam: no-op default + allowlist + scanner hook (V1 fast-follow)."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from mailflow.core.models import Attachment, ScanResult, ScanVerdict
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.safety import (
    AttachmentBlockedError,
    NoOpAttachmentScanner,
    check_allowlist,
)
from mailflow.stores.memory import InMemoryBlobStore

PDF = b"%PDF-1.4 hello"


def _raw_with_pdf() -> bytes:
    m = EmailMessage()
    m["Message-ID"] = "<s.1@example.com>"
    m["From"] = "alice@partner.com"
    m["To"] = "ops@acme.com"
    m["Subject"] = "doc"
    m.set_content("see attached")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="report.pdf")
    return m.as_bytes()


def _extract(extractor: MimeExtractor, raw: bytes):
    return extractor.extract_bytes(
        raw,
        provider="memory",
        provider_message_id="m",
        stream_id="ops@acme.com/Inbox",
        watched_mailbox="ops@acme.com",
        blob_store=InMemoryBlobStore(),
    )


def test_noop_scanner_allows_everything() -> None:
    result = NoOpAttachmentScanner().scan(Attachment(filename="x.exe"))
    assert result.verdict is ScanVerdict.allow


def test_empty_allowlist_allows() -> None:
    att = Attachment(filename="x.exe", content_type="application/x-msdownload")
    assert check_allowlist(att, frozenset()).verdict is ScanVerdict.allow


def test_allowlist_blocks_non_listed_type() -> None:
    att = Attachment(filename="x.exe", content_type="application/x-msdownload")
    assert check_allowlist(att, frozenset({"pdf", "application/pdf"})).verdict is ScanVerdict.block


def test_default_extractor_keeps_attachment() -> None:
    ce = _extract(MimeExtractor(), _raw_with_pdf())
    assert len(ce.attachments) == 1
    assert ce.attachments[0].content_type == "application/pdf"


def test_extractor_with_allowlist_blocks_message() -> None:
    ext = MimeExtractor(allowlist=frozenset({"text/plain"}))
    with pytest.raises(AttachmentBlockedError):
        _extract(ext, _raw_with_pdf())


class _BlockAll:
    def scan(self, attachment: Attachment) -> ScanResult:
        return ScanResult(verdict=ScanVerdict.block, reason="quarantined by test scanner")


def test_extractor_with_blocking_scanner_blocks_message() -> None:
    ext = MimeExtractor(scanner=_BlockAll())
    with pytest.raises(AttachmentBlockedError):
        _extract(ext, _raw_with_pdf())
```

- [ ] **Step 6: Run, confirm it fails for the right reason.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/extract/test_attachment_safety.py -q`
  Expected: import error — `ModuleNotFoundError: No module named 'mailflow.extract.safety'`.

- [ ] **Step 7: Implement `src/mailflow/extract/safety.py`.**

```python
"""Attachment safety seam (V1 fast-follow): a swappable scanner port default + a
content-type/extension allowlist, run on each attachment BEFORE its bytes are
persisted. V1 ships the SLOT with a no-op scanner and an empty (allow-all)
allowlist; a real AV/CDR scanner is a P2 concern. A blocked attachment fails
closed: the message is dead-lettered (PermanentError -> DLQ)."""

from __future__ import annotations

from mailflow.core.errors import PermanentError
from mailflow.core.models import Attachment, ScanResult, ScanVerdict

# Empty allowlist == no restriction (V1 default leaves current behaviour unchanged).
DEFAULT_ALLOWLIST: frozenset[str] = frozenset()


class AttachmentBlockedError(PermanentError):
    """An attachment failed the allowlist/scan and is quarantined (fail-closed -> DLQ)."""

    def __init__(self, filename: str, reason: str) -> None:
        self.filename = filename
        super().__init__(f"attachment blocked ({filename}): {reason}")


class NoOpAttachmentScanner:
    """V1 default AttachmentScanner: allows everything. The real scanner is P2."""

    def scan(self, attachment: Attachment) -> ScanResult:
        return ScanResult()  # verdict defaults to allow


def check_allowlist(attachment: Attachment, allowlist: frozenset[str]) -> ScanResult:
    """Allow when the attachment's content-type OR lowercased file extension is in
    `allowlist`. An empty allowlist allows everything (no restriction)."""
    if not allowlist:
        return ScanResult()
    name = attachment.filename
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if attachment.content_type.lower() in allowlist or ext in allowlist:
        return ScanResult()
    return ScanResult(
        verdict=ScanVerdict.block,
        reason=f"type not allowlisted: {attachment.content_type} / .{ext}",
    )
```

- [ ] **Step 8: Wire the seam into `MimeExtractor`.** In `src/mailflow/extract/mime.py`:

  (a) Extend the imports added in Task 1 Step 8b — add the scanner port and the safety helpers:
```python
from mailflow.core.models import Attachment, CleanEmail, Direction, Recipient, ScanVerdict
from mailflow.core.ports import AttachmentScanner, BlobStore
from mailflow.extract.safety import (
    DEFAULT_ALLOWLIST,
    AttachmentBlockedError,
    NoOpAttachmentScanner,
    check_allowlist,
)
```
  (b) Replace the Task-1 `__init__` with the full version:
```python
    def __init__(
        self,
        *,
        scanner: AttachmentScanner | None = None,
        allowlist: frozenset[str] = DEFAULT_ALLOWLIST,
        max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
    ) -> None:
        self.scanner: AttachmentScanner = scanner or NoOpAttachmentScanner()
        self.allowlist = allowlist
        self.max_attachment_bytes = max_attachment_bytes
```
  (c) In `_walk_body`, build the attachment metadata first, run allowlist + scanner **before** persisting, and copy in the `storage_ref` afterwards. Replace the Task-1 attachment block with:
```python
            if is_attachment or is_inline_media:
                content_hash, size_bytes = digest_and_size(
                    part, cap=self.max_attachment_bytes
                )
                meta = Attachment(
                    filename=filename or "",
                    content_type=ctype,
                    size_bytes=size_bytes,
                    content_hash=content_hash,
                    content_id=str(cid) if cid else "",
                    is_inline=bool(is_inline_media and not is_attachment),
                )
                # Safety seam (A-safety): allowlist first, then the scanner hook.
                # Runs BEFORE any blob is persisted; a block fails closed -> DLQ.
                result = check_allowlist(meta, self.allowlist)
                if result.verdict is ScanVerdict.allow:
                    result = self.scanner.scan(meta)
                if result.verdict is ScanVerdict.block:
                    raise AttachmentBlockedError(meta.filename, result.reason)
                storage_ref = ""
                if blob_store is not None and size_bytes:
                    storage_ref = blob_store.put_stream(
                        content_hash, iter_decoded(part), ctype
                    )
                attachments.append(meta.model_copy(update={"storage_ref": storage_ref}))
```

- [ ] **Step 9: Run green + mypy.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/extract/test_attachment_safety.py tests/extract/test_mime.py tests/test_pipeline_attachment_failclosed.py -q` → all pass.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` → `Success`.
  (mypy note: `scanner or NoOpAttachmentScanner()` is `AttachmentScanner | NoOpAttachmentScanner`; `NoOpAttachmentScanner` is structurally assignable to the `AttachmentScanner` Protocol, so the `self.scanner: AttachmentScanner` annotation type-checks.)

- [ ] **Step 10: Commit.**
```
git add src/mailflow/extract/safety.py src/mailflow/extract/mime.py tests/extract/test_attachment_safety.py
git commit -m "$(cat <<'EOF'
feat(attach): allowlist + no-op scanner seam, invoked before blob persist

NoOpAttachmentScanner (allow-all) + check_allowlist (empty=allow-all) wired into
MimeExtractor; a block raises AttachmentBlockedError (PermanentError) before any
put_stream, dead-lettering the message via the existing DLQ path. Default
construction keeps behaviour unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — Explicit, tested content-addressed dedupe

`mime.py` already uses the sha256 `content_hash` as the blob `ref`, so same-hash bytes collide on one key. Today that's *accidental* overwrite. Make it **explicit**: each `put_stream` skips re-writing a `ref` that already exists, and prove the same attachment in two messages is stored once.

**Files:**
- **Modify:** `src/mailflow/stores/memory.py` — `InMemoryBlobStore.put_stream` (lines 76–78).
- **Modify:** `src/mailflow/stores/local_blob.py` — `LocalBlobStore.put_stream` (lines 16–21).
- **Create (test):** `tests/stores/test_blob_dedup.py`
- **Create (test):** `tests/extract/test_attachment_dedup_e2e.py`

### Step 1: Store-level dedupe (TDD)

- [ ] **Step 1: Failing store dedupe tests.** Create `tests/stores/test_blob_dedup.py`:

```python
"""Explicit content-addressed dedupe at the BlobStore layer (V1 fast-follow):
an identical ref is written once; a second put_stream is a no-op skip."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from mailflow.stores.local_blob import LocalBlobStore
from mailflow.stores.memory import InMemoryBlobStore


def _counting_chunks(tag: str, log: list[str]) -> Iterator[bytes]:
    log.append(tag)  # runs only when the generator is actually consumed
    yield b"PDFBYTES"


def test_inmemory_skips_rewrite_for_existing_ref() -> None:
    store = InMemoryBlobStore()
    log: list[str] = []
    store.put_stream("hash-abc", _counting_chunks("first", log), "application/pdf")
    store.put_stream("hash-abc", _counting_chunks("second", log), "application/pdf")
    assert log == ["first"]  # second write skipped — generator never consumed
    assert store._blobs == {"hash-abc": b"PDFBYTES"}


def test_local_skips_rewrite_for_existing_ref(tmp_path: Path) -> None:
    store = LocalBlobStore(directory=str(tmp_path))
    log: list[str] = []
    store.put_stream("hash-abc", _counting_chunks("first", log), "application/pdf")
    mtime = os.path.getmtime(tmp_path / "hash-abc")
    store.put_stream("hash-abc", _counting_chunks("second", log), "application/pdf")
    assert log == ["first"]  # second write skipped
    assert os.path.getmtime(tmp_path / "hash-abc") == mtime
```

- [ ] **Step 2: Run, confirm it fails for the right reason.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/stores/test_blob_dedup.py -q`
  Expected: both fail on `assert log == ["first"]` (currently the second `put_stream` consumes its generator and overwrites, so `log == ["first", "second"]`).

- [ ] **Step 3: Add dedupe skip to `InMemoryBlobStore.put_stream`** (`src/mailflow/stores/memory.py`, lines 76–78):
```python
    def put_stream(self, ref: str, chunks: Iterator[bytes], content_type: str) -> str:
        if ref in self._blobs:
            return ref  # content-addressed dedupe: identical bytes already stored, skip
        self._blobs[ref] = b"".join(chunks)
        return ref
```

- [ ] **Step 4: Add dedupe skip to `LocalBlobStore.put_stream`** (`src/mailflow/stores/local_blob.py`, lines 16–21):
```python
    def put_stream(self, ref: str, chunks: Iterator[bytes], content_type: str) -> str:
        path = os.path.join(self.directory, ref)
        if os.path.exists(path):
            return ref  # content-addressed dedupe: identical bytes already stored, skip
        with open(path, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        return ref
```

- [ ] **Step 5: Run green + mypy.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/stores/test_blob_dedup.py -q` → pass.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` → `Success`.

- [ ] **Step 6: Commit.**
```
git add src/mailflow/stores/memory.py src/mailflow/stores/local_blob.py tests/stores/test_blob_dedup.py
git commit -m "$(cat <<'EOF'
feat(attach): explicit content-addressed blob dedupe (skip existing ref)

InMemoryBlobStore and LocalBlobStore now short-circuit put_stream when the ref
already exists, so identical attachment bytes are written once instead of
overwritten. Proven by the unconsumed-generator assertion.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Step 7: End-to-end dedupe proof through the extractor

- [ ] **Step 7: Failing e2e dedupe test.** Create `tests/extract/test_attachment_dedup_e2e.py`:

```python
"""Same attachment in two distinct messages is content-addressed and stored once."""

from __future__ import annotations

from email.message import EmailMessage

from mailflow.extract.mime import MimeExtractor
from mailflow.stores.memory import InMemoryBlobStore

PDF = b"%PDF-1.4 the very same bytes in both messages"


def _raw(mid: str, subject: str) -> bytes:
    m = EmailMessage()
    m["Message-ID"] = f"<{mid}@example.com>"
    m["From"] = "alice@partner.com"
    m["To"] = "ops@acme.com"
    m["Subject"] = subject
    m.set_content("see attached")
    m.add_attachment(PDF, maintype="application", subtype="pdf", filename="report.pdf")
    return m.as_bytes()


def test_same_attachment_two_messages_stored_once() -> None:
    store = InMemoryBlobStore()
    ext = MimeExtractor()
    common = dict(
        provider="memory",
        stream_id="ops@acme.com/Inbox",
        watched_mailbox="ops@acme.com",
        blob_store=store,
    )
    a = ext.extract_bytes(_raw("a1", "first"), provider_message_id="a1", **common)
    b = ext.extract_bytes(_raw("b1", "second"), provider_message_id="b1", **common)
    assert a.attachments[0].content_hash == b.attachments[0].content_hash
    assert a.attachments[0].storage_ref == b.attachments[0].storage_ref
    assert len(store._blobs) == 1  # written once, not overwritten twice
```

- [ ] **Step 8: Run.** With Task-3 store dedupe in place this should pass immediately (the two passes share one `ref`, second `put_stream` skips). It also locks the behaviour against regression.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest tests/extract/test_attachment_dedup_e2e.py -q` → pass.
  If it does **not** pass, the failure is the real bug — investigate before continuing (do not weaken the assertion).

- [ ] **Step 9: Full suite + mypy.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q` → entire suite green.
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy` → `Success: no issues found`.

- [ ] **Step 10: Commit.**
```
git add tests/extract/test_attachment_dedup_e2e.py
git commit -m "$(cat <<'EOF'
test(attach): same attachment across two messages is stored exactly once (e2e)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Run the whole offline suite + strict types one last time.**
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m pytest -q`
  `/Users/iamanam/projects/techjays/poc/mailflow/.venv/bin/python -m mypy`
  Both must be clean. Report verbatim pytest counts and the mypy line in your final message.
- [ ] **Confirm no behaviour drift for default callers:** `builder.py`, `facade.py`, the Gmail composition root, and `registry.py` are untouched and still construct `MimeExtractor()` with all-default args (no-op scanner, empty allowlist, 25 MB cap).

---

## Self-review (spec coverage / placeholder / type-consistency)

**Spec coverage — all three features land and are tested:**
1. *Streaming + fail-closed cap* — `iter_decoded` (chunked decode) + `digest_and_size` (cap) in `streaming.py`; wired in `_walk_body`; unit-tested (chunking/round-trip/over-cap/undecodable) and pipeline-tested (over-cap → exactly one DLQ; under-cap → emits). The fake `iter([payload])` and whole-payload buffering are removed.
2. *Safety seam* — `AttachmentScanner` port + `NoOpAttachmentScanner` + `check_allowlist`; invoked before `put_stream`; tested for no-op allow, allowlist block, and scanner block (both → `AttachmentBlockedError`/DLQ). Slot ships; real scanner deferred to P2.
3. *Explicit dedupe* — both blob stores skip-on-existing-ref; unit test proves single write via an unconsumed generator; e2e test proves the same attachment in two messages yields one blob + shared ref.

**Placeholder scan:** no `TBD`/`add error handling`/`similar to above`/`...`-as-elision. Every code block is complete and matches the real signatures read from source: `extract_bytes(..., blob_store=None, thread_key="")`, `BlobStore.put_stream(ref, chunks, content_type) -> str`, `Attachment` fields (`filename/content_type/size_bytes/content_hash/content_id/is_inline/storage_ref`), `PermanentError`, `MemoryProvider(seed={...})`/`SeedEmail`, `MemoryEmitter`, `RunReport.{emitted,dead_lettered}`.

**Type-consistency (mypy --strict):**
- `import hashlib` is removed from `mime.py` in Task 1 (now only used inside `streaming.py`) — avoids an unused-import error.
- `iter_decoded` narrows `get_payload(decode=False)` with `isinstance(raw, str)` before string ops (the documented union/`Any` gotcha); the `else` branch re-encodes with `latin-1`/`surrogateescape` to stay `bytes`.
- `digest_and_size` returns a concrete `tuple[str, int]` — no `no-any-return`.
- `meta.model_copy(update={...})` returns `Attachment` (pydantic v2 `Self`); typed cleanly.
- `self.scanner: AttachmentScanner = scanner or NoOpAttachmentScanner()` — `NoOpAttachmentScanner` is structurally assignable to the runtime-checkable `AttachmentScanner` Protocol.
- Commit ordering is import-safe: `streaming.py` (imports only `core.errors`) → `mime.py`; `core.models`+`core.ports` additions → `safety.py` → `mime.py` wiring; store edits are leaf changes. The tree stays importable at every commit.

**Contract-decision callout (also surface in the execution report):** over-cap, undecodable, and disallowed/blocked attachments each raise a `PermanentError` subclass so the message is **dead-lettered as a whole** (fail-closed, quarantine semantics) rather than silently dropping the attachment while emitting the message. Downstream impact: a single poison attachment ⇒ exactly one `dead_lettered` count via the existing `_dead_letter` (no new trace), consistent with the DLQ-counting and B1 fail-closed contracts.
