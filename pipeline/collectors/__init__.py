from pipeline.collectors import osm  # noqa: F401 — registers built-in collectors
from pipeline.collectors.base import get_collector

__all__ = ["get_collector"]
