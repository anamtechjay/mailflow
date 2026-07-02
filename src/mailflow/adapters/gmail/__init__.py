"""Google Gmail adapter (per-user OAuth, Pub/Sub push). Reuses the core MIME
parser + extractor — Gmail returns raw RFC822 via messages.get(format=raw)."""

from mailflow.adapters.gmail.client import GmailClient
from mailflow.adapters.gmail.composition import build_gmail_runtime
from mailflow.adapters.gmail.config import GmailConfig, PubSubConfig
from mailflow.adapters.gmail.notifications import parse_pubsub_message
from mailflow.adapters.gmail.provider import GmailProvider
from mailflow.adapters.gmail.runtime import GmailPubSubRuntime
from mailflow.adapters.gmail.watch import GmailWatchManager, WatchHandle

__all__ = [
    "GmailClient", "GmailConfig", "PubSubConfig", "GmailProvider",
    "GmailWatchManager", "WatchHandle", "GmailPubSubRuntime",
    "parse_pubsub_message", "build_gmail_runtime",
]
