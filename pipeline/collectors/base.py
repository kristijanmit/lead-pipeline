"""Collector protocol and registry.

A new source is one class satisfying Collector plus a @register line —
runner.py does not change (docs/ARCHITECTURE.md §7).
"""

from __future__ import annotations

from typing import Callable, Iterable, Protocol

from pipeline.schema import Lead


class Collector(Protocol):
    def collect(self, params: dict) -> Iterable[Lead]: ...


_REGISTRY: dict[str, Callable[[dict], Collector]] = {}


def register(name: str):
    def decorator(cls):
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_collector(name: str, config: dict) -> Collector:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"unknown collector source {name!r} — available: {known}")
    return _REGISTRY[name](config)
