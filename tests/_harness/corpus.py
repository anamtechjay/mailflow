"""Bulk + edge-case email corpus generators for scenario/property tests.

`bulk_seed(n, stream)` gives `build_memory_pipeline` a ready-made seed dict of
`n` distinct messages on one stream. `large_attachment_raw(nbytes)` gives raw
RFC822 bytes whose *wire* size exceeds `nbytes` (base64 + MIME overhead makes
this true for any nbytes, but callers pass generous margins). `TRICKY` is the
edge-case set: a message with no Message-ID, one with no From, and an
HTML-only message (no text/plain part).
"""

from __future__ import annotations

from mailflow.core.models import StreamRef
from mailflow.providers.memory import SeedEmail

from tests._harness.email_builder import Email, raw


def bulk_seed(n: int, stream: StreamRef) -> dict[StreamRef, list[SeedEmail]]:
    """`n` distinct messages ("BULK0".."BULK{n-1}") seeded on a single `stream`."""
    return {
        stream: [
            SeedEmail(f"BULK{i}", raw(f"BULK{i}", subject=f"bulk {i}", body=f"body {i}"))
            for i in range(n)
        ]
    }


def large_attachment_raw(nbytes: int) -> bytes:
    """Raw RFC822 bytes carrying a single attachment of `nbytes` payload bytes
    (base64 encoding + MIME framing means the returned bytes are larger still)."""
    return Email(
        msg_id="LARGE1",
        subject="large attachment",
        body="see attached",
        attachment=("big.bin", b"\x00" * nbytes, "application/octet-stream"),
    ).as_rfc822()


TRICKY: dict[str, bytes] = {
    "no_message_id": Email(
        msg_id="T-NOID", subject="no message id", body="hi", message_id_header=False,
    ).as_rfc822(),
    "missing_from": Email(
        msg_id="T-NOFROM", subject="no from", body="hi", from_header=False,
    ).as_rfc822(),
    "html_only": Email(
        msg_id="T-HTML", subject="html only", html="<html><body><p>Hello <b>world</b></p></body></html>",
    ).as_rfc822(),
}
