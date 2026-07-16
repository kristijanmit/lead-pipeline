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
    "source",
    "contact_emails",
    "contact_phones",
    "lead_type",
    "icp_fit_score",
    "website_audit_score",
    "intent_score",
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
                        lead.source,
                        "; ".join(lead.contact_emails),
                        "; ".join(lead.contact_phones),
                        lead.lead_type or "",
                        _score_cell(lead.icp_fit_score),
                        _score_cell(lead.website_audit_score),
                        lead.intent_score,
                        _score_cell(lead.total_score),
                        lead.status,
                        "; ".join(lead.errors),
                    ]
                )
