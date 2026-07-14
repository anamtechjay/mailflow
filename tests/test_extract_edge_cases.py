"""Comprehensive edge-case & failure-case coverage for the extraction layer.

Complements tests/test_edge_cases.py (filters/stages/facade/state) by exercising the
parts that take UNTRUSTED, MALFORMED input from the wire: the MimeExtractor, the
identity surrogate, attachment handling, and the classification seam.

Organised as:
  A. Identity surrogate (pure functions)        — canonical_id / idempotency / trust
  B. Missing / malformed headers                — no From, no Date, junk Date, no body
  C. Encoding & charset                         — base64 / quoted-printable / RFC2047 / 8bit
  D. Attachments                                — inline vs real, oversize strip, dedup, no name
  E. Classification seam                        — auto-submitted / bounce heuristics
  F. Direction & size                           — inbound/outbound, byte size

Each test states the EXPECTED behaviour so a failure is a real finding, not a surprise.
"""

from __future__ import annotations

import base64

import pytest

from mailflow.core.classification import derive_auto_submitted, derive_is_bounce
from mailflow.core.identity import (
    derive_canonical_id,
    idempotency_key,
    is_message_id_trusted,
    stable_hash,
)
from mailflow.core.models import Direction, StripReason
from mailflow.extract.mime import MimeExtractor
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule
from mailflow.stores.memory import InMemoryBlobStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EX = MimeExtractor()
WATCHED = "ops@acme.com"


def extract(raw: bytes, *, mid: str = "pm1", policy=None, blob=None, mailbox: str = WATCHED):
    ex = MimeExtractor(attachment_policy=policy) if policy is not None else EX
    return ex.extract_bytes(
        raw,
        provider="memory",
        provider_message_id=mid,
        stream_id="Inbox",
        watched_mailbox=mailbox,
        blob_store=blob,
    )


def build(headers: str = "", body: str = "body") -> bytes:
    """Minimal RFC822 with a sensible default header set; `headers` adds/overrides lines."""
    base = (
        "Message-ID: <m@x.com>\r\n"
        "From: alice@partner.com\r\n"
        "To: ops@acme.com\r\n"
        "Subject: hi\r\n"
    )
    return (base + headers + "\r\n" + body).encode()


def raw_no_defaults(headers: str, body: str = "body") -> bytes:
    return (headers + "\r\n" + body).encode()


# ===========================================================================
# A. Identity surrogate — pure functions, the dedupe + canonical-id contract
# ===========================================================================

def test_idempotency_key_composition() -> None:
    assert idempotency_key("acme", "ops@acme.com", "pm1") == "acme|ops@acme.com|pm1"


def test_idempotency_key_differs_per_mailbox() -> None:
    # same provider message in two watched inboxes = two arrivals, NOT one.
    a = idempotency_key("acme", "ops@acme.com", "pm1")
    b = idempotency_key("acme", "sales@acme.com", "pm1")
    assert a != b


def test_stable_hash_is_deterministic_and_distinct() -> None:
    h1 = stable_hash("gmail", "pm1", "ops@acme.com")
    h2 = stable_hash("gmail", "pm1", "ops@acme.com")
    assert h1 == h2 and len(h1) == 64               # sha256 hex
    assert h1 != stable_hash("gmail", "pm2", "ops@acme.com")     # id matters
    assert h1 != stable_hash("graph", "pm1", "ops@acme.com")     # provider matters
    assert h1 != stable_hash("gmail", "pm1", "x@acme.com")       # mailbox matters


@pytest.mark.parametrize("mid,trusted", [
    ("<a@b.com>", True),
    ("  <a@b.com>  ", True),         # surrounding whitespace tolerated
    ("<a-no-domain>", False),        # no @
    ("a@b.com", False),             # no angle brackets
    ("<>", False),                  # empty
    ("", False),
    (None, False),
])
def test_message_id_trust(mid, trusted) -> None:
    assert is_message_id_trusted(mid) is trusted


def test_canonical_id_uses_trusted_message_id() -> None:
    cid, present, trusted = derive_canonical_id(
        provider="gmail", provider_message_id="pm1", mailbox=WATCHED, message_id="<real@x.com>")
    assert cid == "<real@x.com>" and present and trusted


def test_canonical_id_falls_back_to_hash_when_untrusted() -> None:
    cid, present, trusted = derive_canonical_id(
        provider="gmail", provider_message_id="pm1", mailbox=WATCHED, message_id="bogus")
    assert cid == stable_hash("gmail", "pm1", WATCHED)
    assert present is True and trusted is False      # present-but-untrusted


def test_canonical_id_missing_message_id_not_present() -> None:
    cid, present, trusted = derive_canonical_id(
        provider="gmail", provider_message_id="pm1", mailbox=WATCHED, message_id=None)
    assert cid == stable_hash("gmail", "pm1", WATCHED)
    assert present is False and trusted is False


# ===========================================================================
# B. Missing / malformed headers — the extractor must never crash on junk
# ===========================================================================

def test_no_message_id_uses_hash_surrogate() -> None:
    e = extract(raw_no_defaults("From: a@x.com\r\nSubject: hi"))
    assert e.message_id_present is False
    assert e.canonical_id == stable_hash("memory", "pm1", WATCHED)


def test_untrusted_message_id_present_but_not_trusted() -> None:
    # NOTE: build() injects a valid Message-ID, so construct from scratch here.
    e = extract(raw_no_defaults("From: a@x.com\r\nSubject: hi\r\nMessage-ID: no-brackets"))
    assert e.message_id_present is True
    assert e.message_id_trusted is False


def test_no_from_yields_empty_recipient() -> None:
    e = extract(raw_no_defaults("To: ops@acme.com\r\nSubject: hi"))
    assert e.from_.address == ""                     # empty, not a crash


def test_no_subject_is_empty_string() -> None:
    e = extract(raw_no_defaults("From: a@x.com\r\nMessage-ID: <m@x>"))
    assert e.subject == ""


def test_no_date_is_none() -> None:
    e = extract(build())                             # build() has no Date header
    assert e.date_utc is None


def test_malformed_date_does_not_crash() -> None:
    e = extract(build("Date: not-a-real-date\r\n"))
    assert e.date_utc is None                        # unparseable -> None, no exception


def test_valid_date_parsed() -> None:
    e = extract(build("Date: Mon, 30 Jun 2026 10:00:00 +0000\r\n"))
    assert e.date_utc is not None and e.date_utc.year == 2026


def test_empty_body_is_empty_string() -> None:
    e = extract(build(body=""))
    assert e.body_text == ""


def test_headers_only_no_body() -> None:
    e = extract(b"From: a@x.com\r\nSubject: hi\r\nMessage-ID: <m@x>\r\n")
    assert e.body_text == "" and e.attachments == []


def test_plain_text_body_extracted() -> None:
    e = extract(build(body="hello world"))
    assert e.body_text == "hello world"


def test_multiple_from_headers_takes_one() -> None:
    # duplicated From must not crash; one address is chosen.
    e = extract(raw_no_defaults(
        "From: a@x.com\r\nFrom: b@y.com\r\nSubject: hi\r\nMessage-ID: <m@x>"))
    assert e.from_.address in {"a@x.com", "b@y.com"}


def test_size_bytes_equals_raw_length() -> None:
    raw = build(body="measure me")
    e = extract(raw)
    assert e.message_size_bytes == len(raw)


# ===========================================================================
# C. Encoding & charset — decode must produce real text, never raise
# ===========================================================================

def test_quoted_printable_body_decoded() -> None:
    raw = raw_no_defaults(
        "From: a@x.com\r\nSubject: hi\r\nMessage-ID: <m@x>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: quoted-printable",
        body="caf=C3=A9")                             # café
    e = extract(raw)
    assert "café" in e.body_text


def test_base64_body_decoded() -> None:
    payload = base64.b64encode("hello b64".encode()).decode()
    raw = raw_no_defaults(
        "From: a@x.com\r\nSubject: hi\r\nMessage-ID: <m@x>\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: base64",
        body=payload)
    e = extract(raw)
    assert "hello b64" in e.body_text


def test_rfc2047_encoded_subject_decoded() -> None:
    # =?utf-8?b?...?= encoded-word in the Subject must decode to unicode.
    enc = base64.b64encode("Re: café ☕".encode()).decode()
    e = extract(build(f"Subject-x: x\r\nSubject: =?utf-8?b?{enc}?=\r\n").replace(
        b"Subject: hi\r\n", b""))
    assert "café" in e.subject


def test_unknown_charset_does_not_crash() -> None:
    # FIXED (MIME-2): an unknown declared charset no longer raises LookupError; the
    # extractor/envelope fall back to a permissive UTF-8-with-replacement decode so the
    # mail is delivered with a best-effort body. See tests/qa/test_mime2_charset_fallback.py.
    raw = raw_no_defaults(
        "From: a@x.com\r\nSubject: hi\r\nMessage-ID: <m@x>\r\n"
        "Content-Type: text/plain; charset=x-totally-made-up",
        body="still readable")
    e = extract(raw)                                 # no longer raises
    assert "still readable" in e.body_text


def test_html_only_body_falls_back_to_text() -> None:
    raw = raw_no_defaults(
        "From: a@x.com\r\nSubject: hi\r\nMessage-ID: <m@x>\r\n"
        "Content-Type: text/html; charset=utf-8",
        body="<p>Hello <b>bold</b></p>")
    e = extract(raw)
    assert e.body_html != "" and "Hello" in e.body_text     # html_to_text fallback


# ===========================================================================
# D. Attachments — inline vs real, strip-on-oversize, dedup, missing name
# ===========================================================================

def _multipart(part_headers: str, data: bytes, *, text: str = "hello") -> bytes:
    return (
        b"Message-ID: <att@x>\r\nFrom: a@partner.com\r\nTo: ops@acme.com\r\n"
        b"Subject: with-attach\r\n"
        b'Content-Type: multipart/mixed; boundary="BB"\r\n\r\n'
        b"--BB\r\nContent-Type: text/plain\r\n\r\n" + text.encode() + b"\r\n"
        b"--BB\r\n" + part_headers.encode() + b"\r\n\r\n"
        + base64.b64encode(data) + b"\r\n--BB--\r\n"
    )


def test_real_attachment_is_not_inline() -> None:
    raw = _multipart(
        'Content-Type: application/pdf; name="x.pdf"\r\n'
        'Content-Disposition: attachment; filename="x.pdf"\r\n'
        "Content-Transfer-Encoding: base64", b"%PDF-1.4 data", )
    e = extract(raw, blob=InMemoryBlobStore())
    assert len(e.attachments) == 1
    assert e.attachments[0].is_inline is False
    assert e.attachments[0].filename == "x.pdf"
    assert e.attachments[0].content_type == "application/pdf"


def test_inline_image_with_disposition_inline_is_inline() -> None:
    # Realistic Gmail inline image: Content-ID + explicit Content-Disposition: inline.
    raw = _multipart(
        'Content-Type: image/png; name="logo.png"\r\n'
        "Content-ID: <logo1>\r\n"
        "Content-Disposition: inline\r\n"
        "Content-Transfer-Encoding: base64", b"\x89PNG data", )
    e = extract(raw, blob=InMemoryBlobStore())
    assert len(e.attachments) == 1 and e.attachments[0].is_inline is True


@pytest.mark.xfail(
    reason="FINDING E-2: is_inline is computed two different ways. The kept Attachment uses "
           "`is_inline_media and not is_attachment`; policy mode uses "
           "`is_inline_media and disp != 'attachment'`. A Content-ID part that ALSO has a "
           "filename but no Content-Disposition is therefore is_inline=False when delivered "
           "but is_inline=True when stripped — inconsistent classification of the same part.",
    strict=True)
def test_content_id_with_name_no_disposition_inline_is_consistent() -> None:
    raw = _multipart(
        'Content-Type: image/png; name="logo.png"\r\n'
        "Content-ID: <logo1>\r\n"
        "Content-Transfer-Encoding: base64", b"\x89PNG data", )
    e = extract(raw, blob=InMemoryBlobStore())
    # Has a Content-ID -> intuitively inline; currently classified as a real attachment.
    assert e.attachments[0].is_inline is True


def test_attachment_without_filename() -> None:
    raw = _multipart(
        "Content-Type: application/octet-stream\r\n"
        "Content-Disposition: attachment\r\n"
        "Content-Transfer-Encoding: base64", b"raw bytes", )
    e = extract(raw, blob=InMemoryBlobStore())
    assert len(e.attachments) == 1 and e.attachments[0].filename == ""


def test_policy_strips_oversize_attachment_not_dlq() -> None:
    raw = _multipart(
        'Content-Type: image/png; name="big.png"\r\n'
        "Content-ID: <b>\r\n"
        "Content-Transfer-Encoding: base64", b"x" * 50, )
    policy = AttachmentPolicy(inline=AttachmentRule(max_bytes=4))
    e = extract(raw, policy=policy, blob=InMemoryBlobStore())
    assert e.attachments == []                        # stripped
    assert len(e.stripped_attachments) == 1
    assert e.stripped_attachments[0].reason is StripReason.oversize


def test_default_no_policy_keeps_attachment() -> None:
    raw = _multipart(
        'Content-Type: image/png; name="logo.png"\r\n'
        "Content-ID: <b>\r\n"
        "Content-Transfer-Encoding: base64", b"x" * 50, )
    e = extract(raw, blob=InMemoryBlobStore())
    assert len(e.attachments) == 1 and e.stripped_attachments == []


def test_same_attachment_bytes_dedup_to_one_blob() -> None:
    blob = InMemoryBlobStore()
    data = b"identical attachment payload"
    raw = (
        b"Message-ID: <dup@x>\r\nFrom: a@x.com\r\nTo: ops@acme.com\r\n"
        b"Subject: two-same\r\n"
        b'Content-Type: multipart/mixed; boundary="BB"\r\n\r\n'
        b"--BB\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
        b'--BB\r\nContent-Type: application/pdf; name="a.pdf"\r\n'
        b"Content-Disposition: attachment; filename=\"a.pdf\"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + base64.b64encode(data) + b"\r\n"
        b'--BB\r\nContent-Type: application/pdf; name="b.pdf"\r\n'
        b"Content-Disposition: attachment; filename=\"b.pdf\"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + base64.b64encode(data) + b"\r\n"
        b"--BB--\r\n"
    )
    e = extract(raw, blob=blob)
    assert len(e.attachments) == 2                    # both referenced
    # identical content -> same content-addressed ref (stored once)
    assert e.attachments[0].storage_ref == e.attachments[1].storage_ref


def test_policy_mutually_exclusive_with_legacy_knobs() -> None:
    with pytest.raises(ValueError):
        MimeExtractor(attachment_policy=AttachmentPolicy(), max_attachment_bytes=1)


def test_nested_multipart_alternative_extracts_text() -> None:
    raw = (
        b"Message-ID: <nest@x>\r\nFrom: a@x.com\r\nTo: ops@acme.com\r\n"
        b"Subject: nested\r\n"
        b'Content-Type: multipart/mixed; boundary="OUT"\r\n\r\n'
        b"--OUT\r\n"
        b'Content-Type: multipart/alternative; boundary="IN"\r\n\r\n'
        b"--IN\r\nContent-Type: text/plain\r\n\r\nplain version\r\n"
        b"--IN\r\nContent-Type: text/html\r\n\r\n<p>html version</p>\r\n"
        b"--IN--\r\n"
        b"--OUT--\r\n"
    )
    e = extract(raw)
    assert e.body_text == "plain version" and "html version" in e.body_html


# ===========================================================================
# E. Classification seam — auto-submitted & bounce heuristics
# ===========================================================================

@pytest.mark.parametrize("hdr,expected", [
    ("auto-generated", True),
    ("auto-replied", True),
    ("AUTO-GENERATED", True),        # case-insensitive
    ("no", False),                   # explicit no
    (" no ", False),                 # whitespace tolerated
    (None, False),                   # absent
])
def test_derive_auto_submitted(hdr, expected) -> None:
    assert derive_auto_submitted(hdr) is expected


def test_bounce_from_mailer_daemon() -> None:
    assert derive_is_bounce(from_address="MAILER-DAEMON@x.com", return_path=None, content_type=None)


def test_bounce_from_postmaster() -> None:
    assert derive_is_bounce(from_address="postmaster@x.com", return_path=None, content_type=None)


def test_bounce_null_return_path() -> None:
    assert derive_is_bounce(from_address="a@x.com", return_path="<>", content_type=None)
    assert derive_is_bounce(from_address="a@x.com", return_path="", content_type=None)


def test_bounce_delivery_status_report() -> None:
    assert derive_is_bounce(
        from_address="a@x.com", return_path="<a@x.com>",
        content_type="multipart/report; report-type=delivery-status")


def test_normal_mail_is_not_bounce() -> None:
    assert not derive_is_bounce(
        from_address="alice@x.com", return_path="<alice@x.com>", content_type="text/plain")


def test_classification_wired_onto_cleanemail() -> None:
    e = extract(build("Auto-Submitted: auto-generated\r\n"))
    assert e.is_auto_submitted is True
    normal = extract(build())
    assert normal.is_auto_submitted is False and normal.is_bounce is False


# ===========================================================================
# F. Direction & misc
# ===========================================================================

def test_direction_inbound_when_from_is_external() -> None:
    e = extract(build(), mailbox=WATCHED)            # from alice@partner.com != watched
    assert e.direction is Direction.inbound


def test_direction_outbound_when_from_is_watched_mailbox() -> None:
    raw = raw_no_defaults(f"From: {WATCHED}\r\nTo: x@y.com\r\nSubject: hi\r\nMessage-ID: <m@x>")
    e = extract(raw, mailbox=WATCHED)
    assert e.direction is Direction.outbound


def test_direction_outbound_case_insensitive() -> None:
    raw = raw_no_defaults("From: OPS@ACME.COM\r\nTo: x@y.com\r\nSubject: hi\r\nMessage-ID: <m@x>")
    e = extract(raw, mailbox=WATCHED)
    assert e.direction is Direction.outbound          # case-folded match
