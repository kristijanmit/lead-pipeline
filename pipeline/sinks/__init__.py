from pipeline.sinks import notion_sink  # noqa: F401 — registers built-in sinks
from pipeline.sinks.base import get_sink

__all__ = ["get_sink"]
