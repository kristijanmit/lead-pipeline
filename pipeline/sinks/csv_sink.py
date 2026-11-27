"""Human-readable CSV export. A view of the data for sanity-checking and
pasting into Sheets — JSONL stays the source of truth."""

from __future__ import annotations

import csv
from pathlib import Path

from pipeline.schema import Lead

_COLUMNS = [
    "company",
    "domain",
    "industry",
    "location",
    "country",
    "source",
    "contact_emails",
    "contact_phones",
    "lead_type",
    "qualified",
    "tier",
    "priority",
    "need_score",
    "viability_score",
    "reach_score",
    "score_confidence",
    "niche_match",
    "disqualify_reason",
    "score_reasons",
    "reachability_score",
    "opportunity_score",
    "total_score",
    "status",
    "errors",
]


def _score_cell(value: float | None) -> float | str:
    return "" if value is None else value


class CsvSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def write(self, leads: list[Lead]) -> None:
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(_COLUMNS)
            for lead in leads:
                writer.writerow(
                    [
                        lead.company,
                        lead.domain,
                        lead.industry,
                        lead.location,
                        lead.country,
                        lead.source,
                        "; ".join(lead.contact_emails),
                        "; ".join(lead.contact_phones),
                        lead.lead_type or "",
                        lead.qualified,
                        lead.tier or "",
                        _score_cell(lead.priority),
                        _score_cell(lead.need_score),
                        _score_cell(lead.viability_score),
                        _score_cell(lead.reach_score),
                        lead.score_confidence or "",
                        lead.niche_match or "",
                        lead.disqualify_reason or "",
                        " · ".join(lead.score_reasons),
                        _score_cell(lead.reachability_score),
                        _score_cell(lead.opportunity_score),
                        _score_cell(lead.total_score),
                        lead.status,
                        "; ".join(lead.errors),
                    ]
                )
