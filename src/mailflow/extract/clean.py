"""Gentle, stdlib-only content cleaning (A6/A7).

`html_to_text` converts an HTML body to readable plain text without pulling in a
new dependency (no lxml/bs4): strip tags, unescape entities, collapse whitespace.
`ThinContentCleaner` is the default `ContentCleaner` — it backfills an empty
`body_text` from `body_html` and is idempotent. `normalize_subject` produces the
A7 subject-fallback thread key.
"""

from __future__ import annotations

import re
from html import unescape

from mailflow.core.models import CleanEmail

# Drop entire <script>/<style> blocks (content included), case-insensitive, across newlines.
_DROP_BLOCKS = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
# Any remaining tag.
_TAG = re.compile(r"<[^>]+>")
# Runs of whitespace (incl. newlines) collapse to a single space.
_WS = re.compile(r"\s+")
# Leading Re:/Fwd:/Fw: markers, case-insensitive, possibly repeated/bracketed.
_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fwd|fw)\s*:\s*)+", re.IGNORECASE)


def html_to_text(html: str) -> str:
    """Gentle HTML -> text: strip tags, unescape entities, collapse whitespace."""
    if not html:
        return ""
    without_blocks = _DROP_BLOCKS.sub(" ", html)
    without_tags = _TAG.sub(" ", without_blocks)
    text = unescape(without_tags)
    return _WS.sub(" ", text).strip()


def normalize_subject(subject: str) -> str:
    """A7 subject fallback: drop leading Re:/Fwd:, trim, collapse whitespace."""
    if not subject:
        return ""
    stripped = _REPLY_PREFIX.sub("", subject)
    return _WS.sub(" ", stripped).strip()


class ThinContentCleaner:
    """Default `ContentCleaner`: backfill empty body_text from HTML; gentle + idempotent."""

    def clean(self, email: CleanEmail) -> CleanEmail:
        if not email.body_text and email.body_html:
            email.body_text = html_to_text(email.body_html)
        return email
