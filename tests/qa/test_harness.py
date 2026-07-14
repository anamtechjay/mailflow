"""Self-tests for the Phase-0 harness itself (tests/_harness + tests/conftest.py).
Later phases assume these building blocks behave as asserted here."""

from __future__ import annotations

import pytest

from mailflow.core.errors import TransientError
from mailflow.core.models import StreamRef
from mailflow.emit.memory import MemoryEmitter

from tests._harness.corpus import TRICKY, bulk_seed, large_attachment_raw
from tests._harness.email_builder import Email, raw
from tests._harness.fakes import FaultExtractor, _FakeGraphTransport, _graph_message, build_memory_pipeline

S = StreamRef(mailbox="me@acme.com", folder="inbox")


# =========================================================================== 0.1 fixtures


def test_mem_stores_fixture_shape(mem_stores):
    assert set(mem_stores) == {"cursor_store", "dedupe_store", "blob_store"}
    assert mem_stores["cursor_store"].get("acme", S) is None


def test_sqlite_stores_fixture_is_restart_safe(sqlite_stores):
    from mailflow.core.models import Cursor
    sqlite_stores["cursor_store"].commit_if_ahead("acme", S, Cursor(value="1", order=1))
    assert sqlite_stores["cursor_store"].get("acme", S).order == 1


def test_sink_fixture_is_memory_emitter(sink):
    assert isinstance(sink, MemoryEmitter)
    assert sink.events == []


# =========================================================================== 0.2 email_builder


def test_builder_both_shapes():
    e = Email(msg_id="M1", sender="a@partner.com", subject="hi", body="hello")
    assert b"Subject: hi" in e.as_rfc822()
    assert e.as_graph_json()["subject"] == "hi"


def test_builder_raw_shortcut_matches_email_as_rfc822():
    assert raw("M2", subject="x", body="y") == Email(msg_id="M2", subject="x", body="y").as_rfc822()


def test_builder_no_message_id_header_omitted():
    e = Email(msg_id="M3", message_id_header=False)
    assert b"Message-ID" not in e.as_rfc822()
    assert e.as_graph_json()["internetMessageId"] == ""


def test_builder_no_from_header_omitted():
    e = Email(msg_id="M4", from_header=False)
    assert b"From:" not in e.as_rfc822()
    assert e.as_graph_json()["from"] is None


def test_builder_html_only_has_no_plain_part_but_renders_readable_html():
    e = Email(msg_id="M5", html="<p>Hello <b>world</b></p>")
    body = e.as_rfc822()
    assert b"text/html" in body
    assert e.as_graph_json()["body"]["contentType"] == "html"


# =========================================================================== 0.3 corpus


def test_bulk_seed_has_n_entries():
    seed = bulk_seed(500, S)
    assert len(seed[S]) == 500


def test_bulk_seed_entries_are_distinct():
    seed = bulk_seed(5, S)
    ids = [item.provider_message_id for item in seed[S]]
    assert len(set(ids)) == 5


def test_large_attachment_raw_exceeds_requested_size():
    assert len(large_attachment_raw(1_000_000)) > 1_000_000


def test_tricky_corpus_has_expected_cases():
    assert set(TRICKY) == {"no_message_id", "missing_from", "html_only"}
    for raw_bytes in TRICKY.values():
        assert isinstance(raw_bytes, bytes) and raw_bytes


# =========================================================================== 0.4 fakes / build_memory_pipeline


def test_build_memory_pipeline_runs_seeded_messages():
    sink = MemoryEmitter()
    report = build_memory_pipeline(seed=bulk_seed(3, S), emitter=sink).run_once()
    assert report.emitted == 3
    assert len(sink.events) == 3


def test_build_memory_pipeline_defaults_to_fresh_emitter_and_stores():
    pipeline = build_memory_pipeline(seed=bulk_seed(1, S))
    report = pipeline.run_once()
    assert report.emitted == 1
    assert len(pipeline.emitter.events) == 1  # type: ignore[attr-defined]


def test_build_memory_pipeline_observers_on_trace():
    traces = []
    pipeline = build_memory_pipeline(seed=bulk_seed(1, S), observers_on_trace=traces.append)
    pipeline.run_once()
    assert len(traces) == 1


def test_build_memory_pipeline_reuses_given_stores(mem_stores):
    build_memory_pipeline(seed=bulk_seed(1, S), stores=mem_stores).run_once()
    assert mem_stores["cursor_store"].get("acme", S) is not None


def test_fault_extractor_raises_transient_then_succeeds():
    """A TransientError does not retry WITHIN one run_once() — it releases the
    dedupe claim and leaves the cursor un-advanced (not terminal), so the SAME
    message is re-fetched by the next run_once() call (the external retry loop
    a real host process would drive). fail_until=2 succeeds on the 3rd call,
    still within the default max_attempts=3."""
    fx = FaultExtractor(fail_until=2)
    pipeline = build_memory_pipeline(seed=bulk_seed(1, S), extractor=fx)
    reports = [pipeline.run_once() for _ in range(3)]
    assert [r.emitted for r in reports] == [0, 0, 1]
    assert fx.calls == 3  # 2 synthetic failures + 1 success


def test_fault_extractor_raises_transient_error_type():
    from datetime import datetime, timezone

    from mailflow.core.models import Cursor, RawMessage

    seed_email = bulk_seed(1, S)[S][0]
    raw_msg = RawMessage(
        provider="memory", provider_message_id=seed_email.provider_message_id, stream=S,
        size_bytes=len(seed_email.raw), received_at=datetime.now(timezone.utc),
        cursor=Cursor(value="1", order=1), raw_bytes=seed_email.raw,
    )
    fx = FaultExtractor(fail_until=1)
    with pytest.raises(TransientError):
        fx.extract(raw_msg, env=None)  # type: ignore[arg-type]


def test_graph_fake_transport_serves_canned_message():
    transport = _FakeGraphTransport({"MSG1": _graph_message("MSG1", subject="hi")})
    resp = transport.request("GET", "https://graph/v1/users/x/messages/MSG1?$select=x", headers={}, json=None)
    assert resp.status_code == 200
    assert resp.json()["subject"] == "hi"
