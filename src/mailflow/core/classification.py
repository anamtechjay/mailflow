"""Thin message-classification helpers — the boolean seam (§7.4 / R-D3).

Derive cheap boolean signals from already-parsed header values. Deliberately THIN:
the full RFC3464 DSN engine is a P2 concern. Keeping the logic here (one pure module,
no I/O, no vendor imports) lets the MIME + Graph envelope parsers and both extractors
share one definition so the wire stays consistent.
"""

from __future__ import annotations

# Local-parts that conventionally originate auto-generated bounce / failure mail.
_BOUNCE_SENDERS = {"mailer-daemon", "postmaster"}


def derive_auto_submitted(auto_submitted: str | None) -> bool:
    """True when an Auto-Submitted header is present and is not the literal "no"
    (RFC 3834). Mirrors the legacy `auto_submitted.lower() != "no"` filter check."""
    return bool(auto_submitted and auto_submitted.strip().lower() != "no")


def derive_is_bounce(
    *,
    from_address: str,
    return_path: str | None,
    content_type: str | None,
) -> bool:
    """Thin bounce heuristic (seam only — full RFC3464 parsing is P2). True when ANY:
      * the sender local-part is a daemon address (mailer-daemon / postmaster),
      * the Return-Path is the null path (`<>` or empty), or
      * the Content-Type is a delivery-status report
        (multipart/report; report-type=delivery-status).
    """
    local = from_address.split("@", 1)[0].strip().lower()
    if local in _BOUNCE_SENDERS:
        return True
    if return_path is not None and return_path.strip() in ("", "<>"):
        return True
    ct = (content_type or "").lower()
    if "multipart/report" in ct and "delivery-status" in ct:
        return True
    return False
