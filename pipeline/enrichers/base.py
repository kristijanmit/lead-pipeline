"""Enricher protocol, registry, and the per-lead orchestration helper.

A new enrichment step is one class satisfying Enricher plus a @register
line — runner.py does not change (docs/ARCHITECTURE.md §7). Enrichers return a
new Lead, never mutate in place.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Protocol

from pipeline.schema import Lead


class Enricher(Protocol):
    def enrich(self, lead: Lead) -> Lead: ...


_REGISTRY: dict[str, Callable[[dict], Enricher]] = {}


def register(name: str):
    def decorator(cls):
        _REGISTRY[name] = cls
        return cls

    return decorator


def get_enricher(name: str, config: dict) -> Enricher:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"unknown enricher {name!r} — available: {known}")
    return _REGISTRY[name](config)


def apply_enrichers(
    lead: Lead, enrichers: list[tuple[str, Enricher]], retry_delay_s: float = 2.0
) -> Lead:
    """Run each enricher in order on one lead — degrade, don't crash.

    A step that raises is retried once (transient timeouts are the common
    case); a second failure lands in Lead.errors and the remaining steps
    still run with whatever fields already succeeded. Any exhausted retry
    marks the lead enrich_failed so a future re-run picks it up again —
    only status "enriched" is skipped on re-runs (docs/ARCHITECTURE.md §9).
    """
    current = lead
    failed = False
    for name, enricher in enrichers:
        try:
            current = enricher.enrich(current)
        except Exception:
            time.sleep(retry_delay_s)
            try:
                current = enricher.enrich(current)
            except Exception as retry_error:
                failed = True
                current = replace(
                    current, errors=[*current.errors, f"{name}: {retry_error}"]
                )
    return replace(current, status="enrich_failed" if failed else "enriched")
