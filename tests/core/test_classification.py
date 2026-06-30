"""Thin auto-submitted / bounce classification helpers (the boolean seam, RFC3464 is P2)."""

from __future__ import annotations

from mailflow.core.classification import derive_auto_submitted, derive_is_bounce


def test_auto_submitted_present_not_no_is_true() -> None:
    assert derive_auto_submitted("auto-generated") is True
    assert derive_auto_submitted("auto-replied") is True


def test_auto_submitted_no_or_absent_is_false() -> None:
    assert derive_auto_submitted("no") is False
    assert derive_auto_submitted("No") is False
    assert derive_auto_submitted(None) is False
    assert derive_auto_submitted("") is False


def test_bounce_daemon_sender() -> None:
    assert derive_is_bounce(from_address="MAILER-DAEMON@mx.example",
                            return_path=None, content_type=None) is True
    assert derive_is_bounce(from_address="postmaster@mx.example",
                            return_path=None, content_type=None) is True


def test_bounce_null_return_path() -> None:
    assert derive_is_bounce(from_address="a@x.com", return_path="<>", content_type=None) is True
    assert derive_is_bounce(from_address="a@x.com", return_path="", content_type=None) is True


def test_bounce_delivery_status_report() -> None:
    ct = "multipart/report; report-type=delivery-status; boundary=xyz"
    assert derive_is_bounce(from_address="bounce@mx.example",
                            return_path="<bounce@mx.example>", content_type=ct) is True


def test_not_bounce_ordinary_mail() -> None:
    assert derive_is_bounce(from_address="alice@x.com",
                            return_path="<alice@x.com>",
                            content_type="text/plain") is False
    # a multipart/report read-receipt (disposition-notification) is NOT a bounce
    assert derive_is_bounce(
        from_address="alice@x.com", return_path="<alice@x.com>",
        content_type="multipart/report; report-type=disposition-notification") is False
