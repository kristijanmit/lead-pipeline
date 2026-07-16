"""Canonical Lead record — every stage reads and writes this shape.

Defined once, here, per ARCHITECTURE.md §5. Bump SCHEMA_VERSION whenever
fields are added or changed; bump SCORING_VERSION whenever the weighting
formula changes, so old JSONL output stays comparable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 3
SCORING_VERSION = 1


@dataclass
class Lead:
    # identity — set at collection, never changes after
    company: str
    domain: str  # normalized: no scheme, no "www.", lowercase — see dedupe.py
    source: str  # "osm" | "maps" | "manual"
    industry: str = "other"

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

    # scoring
    scoring_version: int = SCORING_VERSION
    icp_fit_score: float | None = None
    website_audit_score: float | None = None
    intent_score: float = 0.0
    total_score: float | None = None

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
