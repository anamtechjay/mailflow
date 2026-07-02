"""The orchestrator. Coordinates the ports to honour every §8 invariant.

Per-message order:
  claim (§8.2) -> size guard (§8.6) -> parse (§7.3) -> filter (§7.4)
  -> extract (§7.6) -> emit (§7.7) -> mark done.
Cursor advances on ANY terminal disposition (§8.1), monotonic + single-writer (§8.3).
Poison messages go to the DLQ and the cursor moves past them (§8.4).
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from mailflow.core.errors import AuthError, ConfigError, PermanentError, TransientError
from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.filtering import FilterContext
from mailflow.core.identity import derive_canonical_id, idempotency_key
from mailflow.core.models import (
    CleanEmail,
    Decision,
    Disposition,
    Envelope,
    RawMessage,
    StreamRef,
)
from mailflow.core.observability import (
    DeadLetter,
    DeadLetterRecord,
    DecisionTrace,
    HealthReport,
    RunReport,
    health as _health,
)
from mailflow.core.ports import (
    AuthRefresher,
    BlobStore,
    Classifier,
    ContentCleaner,
    ContentExtractor,
    CursorStore,
    DeadLetterStore,
    DedupeStore,
    Emitter,
    EnvelopeParser,
    Filter,
    MailboxProvider,
)
from mailflow.extract.mime import MimeExtractor
from mailflow.filters.chain import FilterChain

_log = logging.getLogger("mailflow.pipeline")


class PipelineConfig(BaseModel):
    tenant: str
    max_message_bytes: int = 50_000_000
    max_attempts: int = 3
    claim_lease_seconds: int = 300
    done_ttl_seconds: int = 60 * 60 * 24 * 60  # 60 days (spec §11 dedupe ttl)
    on_filtered: Literal["tag", "drop"] = "tag"


def _assert_port(component: object, port: type, role: str) -> None:
    """Fail fast if `component` does not structurally satisfy `port` (A-port check).
    runtime_checkable isinstance only verifies method-name presence — cheap, and run
    once at construction. mypy strict is the real conformance gate; this catches the
    injection seams (overrides=, connect(), hand-built pipelines) mypy can't see."""
    if isinstance(component, port):
        return
    attrs: frozenset[str] = getattr(port, "__protocol_attrs__", frozenset())
    missing = sorted(a for a in attrs if not hasattr(component, a))
    raise ConfigError(
        f"{role} component {type(component).__name__!r} does not satisfy the "
        f"{port.__name__} port (missing: {missing or 'unknown'})"
    )


class Pipeline:
    def __init__(
        self,
        *,
        provider: MailboxProvider,
        parser: EnvelopeParser,
        filters: FilterChain,
        extractor: ContentExtractor | MimeExtractor,
        emitter: Emitter,
        dlq_emitter: Emitter,
        cursor_store: CursorStore,
        dedupe_store: DedupeStore,
        blob_store: BlobStore,
        config: PipelineConfig,
        classifier: Classifier | None = None,
        cleaner: ContentCleaner | None = None,
        auth_refresher: AuthRefresher | None = None,
        dlq_store: DeadLetterStore | None = None,
    ) -> None:
        _assert_port(provider, MailboxProvider, "provider")
        _assert_port(parser, EnvelopeParser, "parser")
        for _f in filters.filters:
            _assert_port(_f, Filter, "filter")
        if not isinstance(extractor, (ContentExtractor, MimeExtractor)):
            _assert_port(extractor, ContentExtractor, "extractor")
        _assert_port(emitter, Emitter, "emitter")
        _assert_port(dlq_emitter, Emitter, "dlq_emitter")
        _assert_port(cursor_store, CursorStore, "cursor_store")
        _assert_port(dedupe_store, DedupeStore, "dedupe_store")
        _assert_port(blob_store, BlobStore, "blob_store")
        if classifier is not None:
            _assert_port(classifier, Classifier, "classifier")
        if cleaner is not None:
            _assert_port(cleaner, ContentCleaner, "cleaner")
        if auth_refresher is not None:
            _assert_port(auth_refresher, AuthRefresher, "auth_refresher")
        if dlq_store is not None:
            _assert_port(dlq_store, DeadLetterStore, "dlq_store")
        self.provider = provider
        self.parser = parser
        self.filters = filters
        self.extractor = extractor
        self.emitter = emitter
        self.dlq_emitter = dlq_emitter
        self.cursor_store = cursor_store
        self.dedupe_store = dedupe_store
        self.blob_store = blob_store
        self.config = config
        self.classifier = classifier
        self.cleaner = cleaner
        self.auth_refresher = auth_refresher
        self.dlq_store = dlq_store

    def run_once(self) -> RunReport:
        report = RunReport()
        self.provider.connect()
        for stream in self.provider.sync_streams():
            self._run_stream(stream, report)
        return report

    def _run_stream(self, stream: StreamRef, report: RunReport) -> None:
        cursor = self.cursor_store.get(self.config.tenant, stream)
        for msg in self.provider.fetch(stream, cursor):
            report.fetched += 1
            disposition = self._process(msg, report)
            # §8.1: advance the bookmark on ANY terminal disposition, via monotonic CAS (§8.3).
            if disposition is not None:
                self.cursor_store.commit_if_ahead(self.config.tenant, stream, msg.cursor)

    def _process(self, msg: RawMessage, report: RunReport) -> Disposition | None:
        tenant = self.config.tenant
        key = idempotency_key(tenant, msg.stream.mailbox, msg.provider_message_id)
        canonical_id, _present, _trusted = derive_canonical_id(
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            mailbox=msg.stream.mailbox,
            message_id=None,  # refined after parse; fine for trace/dlq keys
        )

        # §8.2: claim before any spend.
        if not self.dedupe_store.try_claim(key, self.config.claim_lease_seconds):
            self._record(report, self._trace(canonical_id, msg, Disposition.duplicate, "dedupe"))
            return Disposition.duplicate

        attempts = self.dedupe_store.record_attempt(key)

        # §8.6 / §B1: size guard against metadata BEFORE downloading/decoding bytes.
        # Fail closed: an unknown/zero reported size is treated as over-limit (we cannot
        # vouch it is within budget, so we never download it).
        size = self.provider.message_size(msg) or msg.size_bytes
        if size <= 0:
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"size unknown (fail-closed): {size}",
                attempts=attempts, error_class="SizeUnknownError",
            )
        if size > self.config.max_message_bytes:
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=f"oversized: {size} > {self.config.max_message_bytes}",
                attempts=attempts, error_class="OversizedMessageError",
            )

        try:
            return self._do_work(msg, key, report)
        except PermanentError as exc:
            # §A2: will never succeed (403/404/410, invalid base64) -> DLQ, no retry.
            return self._dead_letter(
                canonical_id, msg, key, report, reason=f"{type(exc).__name__}: {exc}",
                attempts=attempts, error_class=type(exc).__name__,
            )
        except AuthError as exc:
            # §A2: 401 -> force ONE refresh and retry the body exactly once; not the loop.
            return self._handle_auth_error(canonical_id, msg, key, report, exc)
        except TransientError as exc:
            # §A2: temporary (429/5xx/network) -> bounded retry, else DLQ.
            return self._retry_or_dead_letter(canonical_id, msg, key, report, attempts, exc)
        except Exception as exc:  # noqa: BLE001 - unknown failures are treated as transient
            return self._retry_or_dead_letter(canonical_id, msg, key, report, attempts, exc)

    def _do_work(self, msg: RawMessage, key: str, report: RunReport) -> Disposition:
        tenant = self.config.tenant
        env = self.parser.parse_envelope(msg, tenant)
        decision = self.filters.run(env, FilterContext(tenant=tenant))

        if decision.decision is Decision.drop:
            if self.config.on_filtered == "drop":
                self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
                self._record(report, self._trace(
                    env.canonical_id, msg, Disposition.dropped, "filter",
                    matched_filter=decision.filter_name, reason=decision.reason,
                ))
                return Disposition.dropped
            # on_filtered == "tag": deliver the matched message, tagged. Fall through to
            # the normal extract -> emit path, but stamp the filter tag on the email below.
            _tag: tuple[str, str] | None = (decision.filter_name, decision.reason)
        else:
            _tag = None

        relevance = None
        if self.classifier is not None and decision.decision is Decision.uncertain:
            relevance = self.classifier.classify(env, FilterContext(tenant=tenant))

        email = self._extract(msg, env)
        if relevance is not None:
            email.relevance = relevance
        email.matched_filter = decision.filter_name
        if _tag is not None:
            email.disposition = "filtered"
            email.matched_filter, email.filter_reason = _tag

        for sa in email.stripped_attachments:
            self._record_stripped(report, DecisionTrace(
                canonical_id=email.canonical_id,
                tenant=tenant,
                stream=msg.stream.key,
                disposition=Disposition.dropped,
                stage="attachment_strip",
                reason=sa.reason.value,
                is_inline=sa.is_inline,
            ))

        event = EmailEvent(
            schema_version=SCHEMA_VERSION,
            tenant=tenant,
            ordering_key=msg.stream.mailbox,
            idempotency_key=key,  # §A4: (tenant, mailbox, provider_message_id) on the wire
            email=email,
        )
        self.emitter.emit(event)
        self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
        self._record(report, self._trace(
            email.canonical_id, msg, Disposition.emitted, "emit",
            relevance_score=(relevance.score if relevance else None),
        ))
        return Disposition.emitted

    def _handle_auth_error(
        self, canonical_id: str, msg: RawMessage, key: str, report: RunReport,
        exc: Exception,
    ) -> Disposition:
        # §A2 refresh-and-retry-ONCE: force one credential refresh, then retry the work
        # body a single time in THIS call. Bounded by straight-line control flow (not a
        # counter), so it can never loop on max_attempts. A second failure dead-letters.
        if self.auth_refresher is not None:
            self.auth_refresher.force_refresh()
        try:
            return self._do_work(msg, key, report)
        except Exception as retry_exc:  # noqa: BLE001 - one shot only; any failure -> DLQ
            return self._dead_letter(
                canonical_id, msg, key, report,
                reason=(
                    f"auth retry failed after refresh "
                    f"(original: {type(exc).__name__}: {exc}): "
                    f"{type(retry_exc).__name__}: {retry_exc}"
                ),
                attempts=1, error_class=type(exc).__name__,
            )

    def _retry_or_dead_letter(
        self, canonical_id: str, msg: RawMessage, key: str, report: RunReport,
        attempts: int, exc: Exception,
    ) -> Disposition | None:
        if attempts >= self.config.max_attempts:
            return self._dead_letter(
                canonical_id, msg, key, report, reason=f"{type(exc).__name__}: {exc}",
                attempts=attempts, error_class=type(exc).__name__,
            )
        # free the claim so a later run/redelivery retries (§8.2 lease semantics).
        self.dedupe_store.release(key)
        return None  # NOT terminal -> cursor does NOT advance past it yet

    def _extract(self, msg: RawMessage, env: Envelope) -> CleanEmail:
        # MimeExtractor exposes extract_bytes (raw RFC822); the generic port is
        # extract(msg, env). This seam dispatches between them so the Graph adapter
        # (Plan 2) can implement extract(msg, env) directly without touching the
        # orchestrator.
        if isinstance(self.extractor, MimeExtractor):
            email = self.extractor.extract_bytes(
                msg.raw_bytes,
                provider=msg.provider,
                provider_message_id=msg.provider_message_id,
                stream_id=msg.stream.key,
                watched_mailbox=msg.stream.mailbox,
                blob_store=self.blob_store,
                thread_key=msg.thread_key,
            )
        else:
            email = self.extractor.extract(msg, env)
        if self.cleaner is not None:
            email = self.cleaner.clean(email)
        return email

    def _dead_letter(
        self, canonical_id: str, msg: RawMessage, key: str, report: RunReport, *,
        reason: str, attempts: int = 0, error_class: str = "",
    ) -> Disposition:
        self.dlq_emitter.emit(  # DLQ is just another Emitter sink in the core spine
            EmailEvent(
                schema_version=SCHEMA_VERSION,
                tenant=self.config.tenant,
                ordering_key=msg.stream.mailbox,
                email=self._stub_email(canonical_id, msg),
            )
        )
        # ADDITIVE: durable, replayable record. Does NOT touch any counter. Runs BEFORE the
        # irreversible mark_done so a store failure is recoverable: if put() raises, we abort
        # before the message is claimed-as-done, it stays reclaimable, and a later run
        # re-dead-letters it (put is idempotent by record_id, so no duplication).
        if self.dlq_store is not None:
            self.dlq_store.put(
                self._dead_letter_record(
                    canonical_id, msg, key,
                    reason=reason, attempts=attempts, error_class=error_class,
                )
            )
        self.dedupe_store.mark_done(key, self.config.done_ttl_seconds)
        report.add_dead_letter(
            DeadLetter(canonical_id=canonical_id, reason=reason,
                       provider_message_id=msg.provider_message_id)
        )
        self._record(report, self._trace(canonical_id, msg, Disposition.dead_lettered, "dlq", reason=reason))
        return Disposition.dead_lettered

    def _dead_letter_record(
        self, canonical_id: str, msg: RawMessage, key: str, *,
        reason: str, attempts: int, error_class: str,
    ) -> DeadLetterRecord:
        return DeadLetterRecord(
            record_id=key,  # = idempotency_key; re-dead-letter overwrites
            tenant=self.config.tenant,
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            mailbox=msg.stream.mailbox,
            folder=msg.stream.folder,
            canonical_id=canonical_id,
            reason=reason,
            error_class=error_class,
            attempts=attempts,
            size_bytes=msg.size_bytes,
            thread_key=msg.thread_key,
            cursor_value=msg.cursor.value,
            cursor_order=msg.cursor.order,
            received_at=msg.received_at,
            dead_lettered_at=datetime.now(timezone.utc),
            raw_b64=base64.b64encode(msg.raw_bytes).decode("ascii") if msg.raw_bytes else "",
        )

    def _stub_email(self, canonical_id: str, msg: RawMessage) -> CleanEmail:
        return CleanEmail(
            canonical_id=canonical_id,
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            provider_stream_id=msg.stream.key,
            message_size_bytes=msg.size_bytes,
            schema_version=SCHEMA_VERSION,
        )

    def _record(self, report: RunReport, trace: DecisionTrace) -> None:
        report.record(trace)
        _log.info(
            "disposition=%s stage=%s canonical_id=%s stream=%s "
            "matched_filter=%s reason=%s",
            trace.disposition.value, trace.stage, trace.canonical_id,
            trace.stream, trace.matched_filter, trace.reason,
        )

    def _record_stripped(self, report: RunReport, trace: DecisionTrace) -> None:
        """Record one stripped-attachment trace via RunReport.record_stripped().

        Bypasses the disposition-counter logic in record() so that
        emitted/dropped/duplicates/dead_lettered are not perturbed by strips."""
        report.record_stripped(trace)
        _log.info(
            "disposition=%s stage=%s canonical_id=%s stream=%s "
            "reason=%s is_inline=%s",
            trace.disposition.value, trace.stage, trace.canonical_id,
            trace.stream, trace.reason, trace.is_inline,
        )

    def health(self) -> HealthReport:
        """Reachability of this pipeline's core store dependencies (ops probe)."""
        return _health(
            cursor_store=self.cursor_store,
            dedupe_store=self.dedupe_store,
            blob_store=self.blob_store,
        )

    def _trace(
        self, canonical_id: str, msg: RawMessage, disposition: Disposition, stage: str,
        *, matched_filter: str = "", reason: str = "", relevance_score: float | None = None,
    ) -> DecisionTrace:
        return DecisionTrace(
            canonical_id=canonical_id,
            tenant=self.config.tenant,
            stream=msg.stream.key,
            disposition=disposition,
            stage=stage,
            matched_filter=matched_filter,
            reason=reason,
            relevance_score=relevance_score,
        )
