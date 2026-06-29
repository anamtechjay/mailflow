"""Edge cases across filters, stages, facade, retrieval, and state resolution."""

from __future__ import annotations

import pytest

from mailflow import connect
from mailflow.core.errors import ConfigError
from mailflow.core.filtering import FilterContext
from mailflow.core.models import Decision, Envelope, Recipient, StreamRef
from mailflow.config.state import resolve_state
from mailflow.emit.memory import MemoryEmitter
from mailflow.facade import Mailflow, normalize_filters
from mailflow.filters.deterministic import (
    BlacklistFilter,
    NoPersonalFilter,
    SubjectFilter,
    WhitelistFilter,
    _domain,
)
from mailflow.providers.memory import SeedEmail
from mailflow.stores.memory import InMemoryCursorStore

CTX = FilterContext(tenant="t")
STREAM = StreamRef(mailbox="ops@acme.com", folder="Inbox")


def _env(address: str, subject: str = "hi") -> Envelope:
    return Envelope(canonical_id="c", provider="memory", provider_message_id="m",
                    stream=STREAM, from_=Recipient(address=address), subject=subject)


def _raw(mid: str, sender: str, subject: str = "hi") -> bytes:
    return (f"Message-ID: <{mid}@x>\r\nFrom: {sender}\r\n"
            f"To: ops@acme.com\r\nSubject: {subject}\r\n\r\nbody").encode()


# ---- filter edge cases ----

def test_empty_blacklist_drops_nothing() -> None:
    assert BlacklistFilter(domains=set()).evaluate(_env("a@x.com"), CTX).decision is Decision.uncertain


def test_empty_whitelist_keeps_nothing_by_itself() -> None:
    assert WhitelistFilter(domains=set()).evaluate(_env("a@x.com"), CTX).decision is Decision.uncertain


def test_address_without_at_sign() -> None:
    assert _domain("weird-no-at") == ""
    assert NoPersonalFilter().evaluate(_env("weird-no-at"), CTX).decision is Decision.uncertain


def test_domain_case_insensitive() -> None:
    assert BlacklistFilter(domains={"Spam.COM"}).evaluate(_env("a@SPAM.com"), CTX).decision is Decision.drop


def test_subject_regex_filter() -> None:
    f = SubjectFilter(patterns=[r"(?i)unsubscribe"])
    assert f.evaluate(_env("a@x.com", "Please UNSUBSCRIBE"), CTX).decision is Decision.drop
    assert f.evaluate(_env("a@x.com", "hello"), CTX).decision is Decision.uncertain


def test_whitelist_keep_short_circuits_blacklist() -> None:
    # whitelist matches first -> KEEP wins, the later blacklist never runs
    mf = connect("memory",
                 seed={STREAM: [SeedEmail("m1", _raw("m1", "a@partner.com"))]}, tenant="acme",
                 filters=[{"kind": "whitelist", "domains": ["partner.com"]},
                          {"kind": "blacklist", "domains": ["partner.com"]}])
    assert len(mf.fetch_new()) == 1                # kept by whitelist despite blacklist


def test_filter_order_first_drop_wins() -> None:
    mf = connect("memory",
                 seed={STREAM: [SeedEmail("m1", _raw("m1", "a@spam.com"))]}, tenant="acme",
                 filters=[{"kind": "blacklist", "domains": ["spam.com"]}])
    assert mf.fetch_new() == []


# ---- normalize_filters edge cases ----

def test_normalize_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        normalize_filters([{"kind": "bogus"}])


def test_normalize_params_nested_or_flat() -> None:
    # both {"kind","domains"} (flat) and {"kind","params":{...}} should work
    flat = normalize_filters([{"kind": "blacklist", "domains": ["a.com"]}])[0]
    nested = normalize_filters([{"kind": "blacklist", "params": {"domains": ["a.com"]}}])[0]
    assert flat.name == nested.name == "blacklist"


# ---- stage edge cases ----

def test_empty_stages_pass_through() -> None:
    mf = connect("memory", seed={STREAM: [SeedEmail("m1", _raw("m1", "a@x.com"))]},
                 tenant="acme", stages=[])
    assert len(mf.fetch_new()) == 1


def test_stage_returning_true_keeps_email() -> None:
    mf = connect("memory", seed={STREAM: [SeedEmail("m1", _raw("m1", "a@x.com", "keep"))]},
                 tenant="acme", stages=[lambda e: True])
    out = mf.fetch_new()
    assert len(out) == 1 and out[0].subject == "keep"


def test_clean_fn_then_stages_order() -> None:
    mf = connect("memory", seed={STREAM: [SeedEmail("m1", _raw("m1", "a@x.com"))]}, tenant="acme",
                 clean_fn=lambda e: e.model_copy(update={"subject": "A"}),
                 stages=[lambda e: e.model_copy(update={"subject": e.subject + "B"})])
    assert mf.fetch_new()[0].subject == "AB"          # clean_fn first, then stage


# ---- facade edge cases ----

def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError):
        connect("imap")


def test_fetch_new_requires_queue_output() -> None:
    mf = connect("memory", seed={STREAM: [SeedEmail("m1", _raw("m1", "a@x.com"))]},
                 tenant="acme", on_email=lambda e: None)
    with pytest.raises(RuntimeError):
        mf.fetch_new()                                # on_email used the callback emitter


def test_filters_and_stages_combined() -> None:
    seed = {STREAM: [
        SeedEmail("m1", _raw("m1", "a@partner.com", "ok")),
        SeedEmail("m2", _raw("m2", "b@gmail.com", "ok")),
    ]}
    mf = connect("memory", seed=seed, tenant="acme",
                 filters=[{"kind": "no_personal"}],
                 stages=[lambda e: e.model_copy(update={"subject": "S"})])
    out = mf.fetch_new()
    assert [e.from_.address for e in out] == ["a@partner.com"]   # gmail filtered
    assert out[0].subject == "S"                                  # stage applied


# ---- retrieval edge cases ----

def test_get_email_propagates_fetcher_error() -> None:
    def boom(_mid: str):
        raise RuntimeError("fetch failed")
    mf = Mailflow(provider_kind="gmail", emitter=MemoryEmitter(),
                  cursor_store=InMemoryCursorStore(), fetcher=boom)
    with pytest.raises(RuntimeError):
        mf.get_email("m1")


# ---- state resolution edge cases ----

def test_resolve_state_unknown_scheme_raises() -> None:
    with pytest.raises(NotImplementedError):
        resolve_state("postgres://localhost/db")


def test_resolve_state_sqlite_empty_path_raises() -> None:
    with pytest.raises(ValueError):
        resolve_state("sqlite://")


def test_sqlite_state_dedup_persists_across_restart(tmp_path) -> None:
    db = f"sqlite:///{tmp_path}/mf.db"
    seed = {STREAM: [SeedEmail("m1", _raw("m1", "a@x.com"))]}
    first = connect("memory", seed=seed, tenant="acme", state=db).fetch_new()
    assert len(first) == 1
    # new handle, same db file -> cursor + dedup persisted -> nothing re-emitted
    second = connect("memory", seed=seed, tenant="acme", state=db).fetch_new()
    assert second == []
