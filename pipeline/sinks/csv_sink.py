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
    "contact_phone",
    "total_score",
    "status",
    "errors",
]


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
                        lead.contact_phone or "",
                        "" if lead.total_score is None else lead.total_score,
                        lead.status,
                        "; ".join(lead.errors),
                    ]
                )
