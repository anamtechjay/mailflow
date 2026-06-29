from mailflow.extract.clean import html_to_text


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
