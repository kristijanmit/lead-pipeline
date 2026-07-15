"""Manual CSV collector — LinkedIn/Clutch/referral leads gathered by hand.

Reads leads_manual.csv (see leads_manual.csv.example for the header) so
hand-found leads flow through the same dedupe/enrich/score path as the
automated sources instead of living in a separate spreadsheet.

Columns: company (required), website, industry, emails (";"-separated), phone.
"""

from __future__ import annotations

import csv
from pathlib import Path

from pipeline.collectors.base import register
from pipeline.dedupe import normalize_domain
from pipeline.schema import Lead

_REQUIRED_COLUMNS = {"company"}
_KNOWN_COLUMNS = {"company", "website", "industry", "emails", "phone"}


def read_manual_csv(path: str | Path) -> list[Lead]:
    path = Path(path)
    with open(path, encoding="utf-8-sig", newline="") as f:  # -sig: tolerate Excel BOM
        reader = csv.DictReader(f)
        header = set(reader.fieldnames or [])
        missing = _REQUIRED_COLUMNS - header
        if missing:
            raise ValueError(
                f"{path}: missing column(s) {sorted(missing)} — expected header: "
                f"{','.join(sorted(_KNOWN_COLUMNS))}"
            )
        leads: list[Lead] = []
        for row in reader:
            company = (row.get("company") or "").strip()
            if not company:
                continue  # blank/partial rows are common in hand-edited CSVs
            emails = [
                e.strip() for e in (row.get("emails") or "").split(";") if e.strip()
            ]
            leads.append(
                Lead(
                    company=company,
                    domain=normalize_domain(row.get("website")),
                    source="manual",
                    industry=(row.get("industry") or "").strip() or "other",
                    contact_emails=emails,
                    contact_phone=(row.get("phone") or "").strip() or None,
                )
            )
    return leads


@register("manual")
class ManualCollector:
    def __init__(self, config: dict):
        self.default_path: str = config.get("collect", {}).get(
            "manual_csv", "leads_manual.csv"
        )

    def collect(self, params: dict) -> list[Lead]:
        path = Path(params.get("input") or self.default_path)
        if not path.exists():
            raise ValueError(
                f"manual leads file not found: {path} — copy "
                "leads_manual.csv.example to leads_manual.csv or pass --input"
            )
        return read_manual_csv(path)
