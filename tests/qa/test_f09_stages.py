"""F09 — Cleaning + stages (unit + integration). See docs/qa-partA-coverage.md."""

from __future__ import annotations

from mailflow import connect
from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

from tests._harness.email_builder import raw

S = StreamRef(mailbox="ops@acme.com", folder="inbox")


def test_clean_fn_before_stages():
    # facade.connect(): all_stages = [clean_fn] + stages, so clean_fn always runs
    # first. Prove ordering by having the stage assert on clean_fn's output.
    def clean_fn(email):
        email.body_text = "CLEANED"
        return email

    seen_by_stage: list[str] = []

    def stage(email):
        seen_by_stage.append(email.body_text)  # must already be "CLEANED"
        email.subject = email.subject + "|staged"
        return email

    seed = {S: [SeedEmail("m1", raw("m1", subject="hi"))]}
    mf = connect("memory", seed=seed, clean_fn=clean_fn, stages=[stage])
    emails = mf.fetch_new()

    assert seen_by_stage == ["CLEANED"]
    assert len(emails) == 1
    assert emails[0].body_text == "CLEANED"
    assert emails[0].subject == "hi|staged"


def test_stage_transforms():
    def uppercase_subject(email):
        email.subject = email.subject.upper()
        return email

    seed = {S: [SeedEmail("m1", raw("m1", subject="quarterly report"))]}
    mf = connect("memory", seed=seed, stages=[uppercase_subject])
    emails = mf.fetch_new()

    assert len(emails) == 1
    assert emails[0].subject == "QUARTERLY REPORT"


def test_cleaner_raise_contained():
    # A raising clean_fn is not a documented Stage outcome; it propagates out of
    # StagesEmitter.emit() -> Pipeline._do_work -> caught by _process's catch-all
    # `except Exception` -> treated as transient (contained: no crash, not
    # delivered, claim released for a later retry).
    def _boom(email):
        raise ValueError("clean_fn blew up")

    reports = []
    seed = {S: [SeedEmail("m1", raw("m1"))]}
    mf = connect("memory", seed=seed, clean_fn=_boom, on_report=reports.append)
    emails = mf.fetch_new()

    assert emails == []
    assert len(reports) == 1
    assert reports[0].emitted == 0
    assert reports[0].dead_lettered == 0
