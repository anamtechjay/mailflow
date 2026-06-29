from mailflow.core.models import CleanEmail
from mailflow.core.ports import ContentCleaner
from mailflow.extract.clean import ThinContentCleaner, html_to_text


def _email(**kw: object) -> CleanEmail:
    base: dict[str, object] = dict(
        canonical_id="c1", provider="memory", provider_message_id="m1",
        provider_stream_id="ops@acme.com/Inbox",
    )
    base.update(kw)
    return CleanEmail(**base)  # type: ignore[arg-type]


def test_thin_cleaner_is_a_content_cleaner():
    assert isinstance(ThinContentCleaner(), ContentCleaner)


def test_thin_cleaner_backfills_empty_body_text_from_html():
    out = ThinContentCleaner().clean(_email(body_html="<p>Hello <b>world</b></p>"))
    assert "Hello world" in out.body_text


def test_thin_cleaner_does_not_clobber_existing_body_text():
    out = ThinContentCleaner().clean(
        _email(body_text="real text", body_html="<p>other</p>")
    )
    assert out.body_text == "real text"


def test_thin_cleaner_is_idempotent():
    cleaner = ThinContentCleaner()
    once = cleaner.clean(_email(body_html="<p>Hello <b>world</b></p>"))
    twice = cleaner.clean(once)
    assert twice.body_text == once.body_text


def test_html_to_text_strips_tags_unescapes_entities_collapses_whitespace():
    html = "<p>Hello&nbsp;<b>world</b></p>\n\n<p>Caf&eacute; &amp; tea</p>"
    out = html_to_text(html)
    assert "<" not in out and ">" not in out
    assert "Hello world" in out
    assert "Café & tea" in out
    # collapsed: no runs of whitespace
    assert "  " not in out


def test_html_to_text_empty_is_empty():
    assert html_to_text("") == ""
