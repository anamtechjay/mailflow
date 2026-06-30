"""mailflow — email ingestion toolkit (core spine)."""

from mailflow.builder import build_from_config
from mailflow.config.schema import MailflowConfig
from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail, StrippedAttachment, StripReason
from mailflow.core.pipeline import Pipeline, PipelineConfig
from mailflow.extract.policy import AttachmentPolicy, AttachmentRule
from mailflow.facade import Mailflow, as_cleaner, as_filter, connect

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "CleanEmail",
    "EmailEvent",
    "SCHEMA_VERSION",
    "Pipeline",
    "PipelineConfig",
    "build_from_config",
    "MailflowConfig",
    "connect",
    "Mailflow",
    "as_filter",
    "as_cleaner",
    "AttachmentPolicy",
    "AttachmentRule",
    "StrippedAttachment",
    "StripReason",
]
