# Cowboy Logistics golden corpus

Real email used as a golden-master fixture for the mailflow core spine.

## Provenance
- Source: `email_2703_individual_emails.json` (real Gmail export, received by Cowboy Logistics).
- One Gmail thread, `thread_id = 19afd929badad6c6`: a 17-message freight-quote conversation
  between `sandyqatestacc@yahoo.com` (customer "Peter England Logistics" / Sandy QA) and
  `testuser1@cowboyslogistics.com` (Cowboy Logistics).
- Watched mailbox for direction: **`testuser1@cowboyslogistics.com`**.

## Files
- `corpus.json` — 17 records, trimmed to the fields the fixture loader needs
  (`provider_message_id`, `message_id_header`, `in_reply_to`, `references`, `from_email`,
  `to_email`, `cc_emails`, `subject`, `date`, `attachments`, `body`).
- `golden_cleanemails.json` — the **authored expected** `CleanEmail` projection, keyed by
  Message-ID. Authored by reading the emails directly from the convenience fields, independent
  of the extractor under test (which parses reconstructed RFC822). Fields captured:
  `canonical_id, message_id, message_id_trusted, in_reply_to, references, direction, from, to,
  cc, subject, body_text, attachments, list_id, auto_submitted`.

## Properties worth knowing
- 7 outbound (from Cowboy), 10 inbound (from the customer).
- All 17 Message-IDs are well-formed `<...@...>` ⇒ `message_id_trusted = true`,
  `canonical_id == message_id`.
- References chains grow to depth 16 — a real deep thread; the pipeline carries them verbatim.
- No attachments in this thread; `body_text` keeps quoted history (quote isolation is out of
  scope, spec OD-3). Attachment real-vs-inline splitting is covered by synthetic fixtures in
  plan Task 7.

## Regenerating the golden
The golden is a frozen artifact. If a genuine extractor change alters correct output, update
`golden_cleanemails.json` deliberately and note the reason here — never auto-overwrite it from
the extractor (that would make the test circular).
