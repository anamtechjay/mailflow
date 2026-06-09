"""mailflow — email ingestion toolkit (core spine)."""

from mailflow.builder import build_from_config
from mailflow.config.schema import MailflowConfig
from mailflow.core.events import SCHEMA_VERSION, EmailEvent
from mailflow.core.models import CleanEmail
from mailflow.core.pipeline import Pipeline, PipelineConfig

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
]
