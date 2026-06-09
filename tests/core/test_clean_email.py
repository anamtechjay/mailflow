from mailflow.core.models import CleanEmail, Direction, Recipient, Relevance, Verdict


def test_clean_email_minimal_construction():
    ce = CleanEmail(
        canonical_id="<a@x.com>",
        provider="memory",
        provider_message_id="m1",
        provider_stream_id="ops@x.com:Inbox",
        from_=Recipient(address="a@x.com"),
        subject="hi",
    )
    assert ce.canonical_id == "<a@x.com>"
    assert ce.direction is Direction.unknown
    assert ce.attachments == []
    assert ce.labels == [] and ce.categories == []
    assert ce.relevance.verdict is Verdict.unknown


def test_clean_email_from_alias_serialises_to_from():
    ce = CleanEmail(
        canonical_id="c",
        provider="memory",
        provider_message_id="m1",
        provider_stream_id="s",
        from_=Recipient(address="a@x.com"),
    )
    dumped = ce.model_dump(by_alias=True)
    assert dumped["from"]["address"] == "a@x.com"
    assert "from_" not in dumped


def test_relevance_round_trips():
    r = Relevance(verdict=Verdict.relevant, score=0.9, reason="looks like a customer")
    assert r.score == 0.9 and r.verdict is Verdict.relevant
