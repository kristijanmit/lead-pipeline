"""Canonical Lead record — every stage reads and writes this shape.

Defined once, here, per docs/ARCHITECTURE.md §5. Bump SCHEMA_VERSION whenever
fields are added or changed; bump SCORING_VERSION whenever the weighting
formula changes, so old JSONL output stays comparable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 6
SCORING_VERSION = 3


@dataclass
class Lead:
    # identity — set at collection, never changes after
    company: str
    domain: str  # normalized: no scheme, no "www.", lowercase — see dedupe.py
    source: str  # "osm" | "maps" | "manual"
    industry: str = "other"
    location: str = ""  # the market this lead was collected in, e.g. "Novi Sad" (v5) —
    # stamped by cmd_collect from --location unless the collector set one itself
    # (the manual CSV's optional "location" column). v4 JSONL loads as "".
    country: str = ""  # v6 — stamped by cmd_collect the same way as location; the
    # qualification filter's location gate checks this, not the free-text city

    schema_version: int = SCHEMA_VERSION

    # contact enrichment (Phase 2, step 1)
    contact_emails: list[str] = field(default_factory=list)  # keep every address found
    contact_phones: list[str] = field(default_factory=list)  # keep every number found
    social_links: dict[str, str] = field(default_factory=dict)  # {"linkedin": "...", ...}

    # website audit (Phase 2, step 2)
    https: bool | None = None
    mobile_friendly: bool | None = None
    cms: str | None = None
    lighthouse: dict[str, int] | None = None  # {"performance": 42, "accessibility": 88, ...}
    audit_notes: str = ""

    # scoring (docs/SCORING.md) — reachability_score/opportunity_score/total_score/
    # qualified are all None/False until a lead passes the qualification filters
    # and gets scored; see scorer.py
    scoring_version: int = SCORING_VERSION
    reachability_score: float | None = None
    opportunity_score: float | None = None
    total_score: float | None = None
    lead_type: str | None = None  # "new_build" | "redesign" | "unclear" — set by
    # scorer.py alongside opportunity_score (same _audit_case branch, different
    # pitch implication). Never set at collection/enrichment time. v3 JSONL loads
    # as None via the default — re-running score() fills it in (docs/SCORING.md §8).
    qualified: bool = False  # industry + country filters passed AND reachability_score > 0

    # pipeline bookkeeping
    run_id: str = ""
    status: str = "collected"  # collected | enriched | enrich_failed | scored | synced
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Lead:
        # schema v2 stored a single contact_phone string
        if data.get("contact_phone") and not data.get("contact_phones"):
            data = {**data, "contact_phones": [data["contact_phone"]]}
        # v6 renamed website_audit_score -> opportunity_score
        if data.get("website_audit_score") is not None and data.get("opportunity_score") is None:
            data = {**data, "opportunity_score": data["website_audit_score"]}
        # Tolerate unknown keys so newer JSONL files load under older code
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def write_jsonl(path: str | Path, leads: Iterable[Lead]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8") as f:
        for lead in leads:
            f.write(json.dumps(lead.to_dict(), ensure_ascii=False) + "\n")
            count += 1
    return count


def read_jsonl(path: str | Path) -> list[Lead]:
    leads = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                leads.append(Lead.from_dict(json.loads(line)))
    return leads
