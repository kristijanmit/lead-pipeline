"""Sink protocol and registry.

A new output target is one class satisfying Sink plus a @register line —
runner.py does not change (docs/ARCHITECTURE.md §7).
"""

from __future__ import annotations

from typing import Callable, Protocol

from pipeline.schema import Lead


class Sink(Protocol):
    def write(self, leads: list[Lead]) -> None: ...


_REGISTRY: dict[str, Callable[[dict], Sink]] = {}


def register(name: str):
    def decorator(cls):
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_sink(name: str, config: dict) -> Sink:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"unknown sink {name!r} — available: {known}")
    return _REGISTRY[name](config)
