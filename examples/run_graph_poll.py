"""Minimal poll-runner: drive the Graph adapter to fetch mail and emit CleanEmails.

This is the smallest "live" loop you can build on the Plan 2 library: it polls a
mailbox with `pipeline.run_once()` (a delta fetch), and every fetched message flows
through claim -> filter -> extract -> emit as a CleanEmail. The durable SQLite cursor
means each poll resumes where the last one stopped. NO webhook server / scheduler
needed (those are the Plan 4 push path).

Two transports, selected by flag:

  --demo
      Use a FakeGraphTransport seeded with sample messages. Runs TODAY with zero
      Azure, zero network. Proves the fetch -> clean -> emit path end to end.

  (default / real)
      Use HttpxGraphTransport against real Outlook. Requires:
        * pip install mailflow[graph]          (httpx)
        * export MAILFLOW_GRAPH_TOKEN=<token>   (an app-only bearer token)
        * --mailbox user@tenant.com
        * Azure: app registration + admin consent + Mail.Read + RBAC (Phase 0)

Run from the repo root with the src layout on the path:

    PYTHONPATH=src .venv/bin/python examples/run_graph_poll.py --demo
    PYTHONPATH=src .venv/bin/python examples/run_graph_poll.py --mailbox me@corp.com --loop 30
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time

from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.emit.memory import MemoryEmitter
from mailflow.filters.chain import FilterChain
from mailflow.providers.graph.envelope import GraphEnvelopeParser
from mailflow.providers.graph.extract import GraphExtractor
from mailflow.providers.graph.provider import GraphProvider
from mailflow.registry import build_blob_store, build_cursor_store, build_dedupe_store


def build_pipeline(transport, *, mailbox, folders, db_path, blob_root, emitter):
    """Wire the Graph adapter + durable stores into a runnable Pipeline.

    Identical for demo and real runs — only the `transport` differs. This is the
    whole integration surface: ~12 lines."""
    return Pipeline(
        provider=GraphProvider(transport, mailbox=mailbox, folders=folders),
        parser=GraphEnvelopeParser(),
        filters=FilterChain([]),  # no filters -> keep everything (add rules here)
        extractor=GraphExtractor(transport, build_blob_store("local", {"root": blob_root})),
        emitter=emitter,
        dlq_emitter=MemoryEmitter(),
        cursor_store=build_cursor_store("sqlite", {"path": db_path}),
        dedupe_store=build_dedupe_store("sqlite", {"path": db_path}),
        blob_store=build_blob_store("local", {"root": blob_root}),
        config=PipelineConfig(tenant="demo"),
    )


def demo_transport(mailbox):
    """A FakeGraphTransport seeded with two sample messages (one plain, one HTML
    with a small inline attachment). Keyed on bare paths — the fake path-strips
    query params, so $select/$top don't need to be reproduced here."""
    from mailflow.providers.graph.transport import FakeGraphTransport

    delta = f"/users/{mailbox}/mailFolders/Inbox/messages/delta"

    def meta(mid, subject, frm, *, has_att=False):
        m = {
            "id": mid,
            "internetMessageId": f"<{mid}@corp.com>",
            "subject": subject,
            "from": {"emailAddress": {"name": frm.split("@")[0], "address": frm}},
            "toRecipients": [{"emailAddress": {"address": mailbox}}],
            "bodyPreview": subject + " ...",
            "receivedDateTime": "2026-06-22T09:00:00Z",
            "sentDateTime": "2026-06-22T08:59:00Z",
            "hasAttachments": has_att,
            "internetMessageHeaders": [{"name": "List-Id", "value": "<news.corp.com>"}]
            if "newsletter" in subject.lower()
            else [],
            "singleValueExtendedProperties": [{"id": "Long 0x0E08", "value": "2048"}],
        }
        return m

    png = base64.b64encode(b"\x89PNG demo bytes").decode()
    return FakeGraphTransport(
        json_responses={
            delta: [
                {
                    "value": [
                        meta("m1", "Q3 freight quote", "ops@vendor.com"),
                        meta("m2", "Weekly newsletter", "news@list.com", has_att=True),
                    ],
                    "@odata.deltaLink": "DELTA_DONE",
                }
            ],
            f"/users/{mailbox}/messages/m1": [
                dict(meta("m1", "Q3 freight quote", "ops@vendor.com"),
                     body={"contentType": "text", "content": "Rate is $1,850 all-in."})
            ],
            f"/users/{mailbox}/messages/m2": [
                dict(meta("m2", "Weekly newsletter", "news@list.com", has_att=True),
                     body={"contentType": "html", "content": "<h1>This week</h1>"})
            ],
            f"/users/{mailbox}/messages/m2/attachments": [
                {"value": [{"id": "att1", "name": "logo.png", "contentType": "image/png",
                            "size": 15, "contentBytes": png, "isInline": True}]}
            ],
        }
    )


def real_transport():
    """The real httpx client. Token comes from MAILFLOW_GRAPH_TOKEN; in production
    you'd pass an MSAL / managed-identity callable instead of reading an env var."""
    try:
        from mailflow.providers.graph.live import HttpxGraphTransport
    except Exception as exc:  # pragma: no cover
        sys.exit(f"Real mode needs the graph extra: pip install mailflow[graph]  ({exc})")
    token = os.environ.get("MAILFLOW_GRAPH_TOKEN")
    if not token:
        sys.exit("Set MAILFLOW_GRAPH_TOKEN=<app-only bearer token> for real mode.")
    return HttpxGraphTransport(token_provider=lambda: token)


def print_emails(emitter, report):
    for event in emitter.events:
        e = event.email
        body = e.body_text or e.body_html
        print(f"  - {e.subject!r} from {e.from_.address}"
              f" | {len(e.attachments)} attachment(s)"
              f" | body: {body[:50]!r}"
              f" | canonical_id={e.canonical_id[:24]}...")
    print(f"  [run] fetched={report.fetched} emitted={len(emitter.events)}"
          f" dead_lettered={report.dead_lettered}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", action="store_true", help="use the seeded fake transport (no Azure)")
    ap.add_argument("--mailbox", default="demo@corp.com", help="mailbox UPN")
    ap.add_argument("--folders", default="Inbox", help="comma-separated folders")
    ap.add_argument("--loop", type=int, default=0, help="poll every N seconds (0 = once)")
    args = ap.parse_args()

    folders = args.folders.split(",")
    if args.demo:
        # Fresh temp dir so the demo is repeatable; real mode uses persistent paths.
        work = tempfile.mkdtemp(prefix="mailflow-demo-")
        db_path, blob_root = f"{work}/cursor.db", f"{work}/blobs"
        transport = demo_transport(args.mailbox)
        print(f"[demo] fake transport, durable stores in {work}")
    else:
        db_path, blob_root = "mailflow.db", "mailflow-blobs"
        transport = real_transport()
        print(f"[real] httpx transport, mailbox={args.mailbox}, stores ./{db_path}")

    while True:
        emitter = MemoryEmitter()
        pipeline = build_pipeline(
            transport, mailbox=args.mailbox, folders=folders,
            db_path=db_path, blob_root=blob_root, emitter=emitter,
        )
        report = pipeline.run_once()
        print_emails(emitter, report)
        if args.loop <= 0:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
