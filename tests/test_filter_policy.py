from mailflow.core.models import CleanEmail


def test_cleanemail_disposition_defaults_to_emitted():
    email = CleanEmail(canonical_id="c1", provider="memory", provider_message_id="m1",
                       provider_stream_id="s1")
    assert email.disposition == "emitted"
    assert email.filter_reason == ""


def test_cleanemail_can_be_marked_filtered():
    email = CleanEmail(canonical_id="c1", provider="memory", provider_message_id="m1",
                       provider_stream_id="s1")
    email.disposition = "filtered"
    email.filter_reason = "list/auto-submitted header present"
    assert email.disposition == "filtered"
    assert email.filter_reason == "list/auto-submitted header present"
