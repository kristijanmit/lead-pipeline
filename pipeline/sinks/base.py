"""Sink protocol (ARCHITECTURE.md §7)."""

from __future__ import annotations

from typing import Protocol

from pipeline.schema import Lead


class Sink(Protocol):
    def write(self, leads: list[Lead]) -> None: ...
