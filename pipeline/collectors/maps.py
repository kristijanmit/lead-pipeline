"""Google Maps collector — wraps gosom/google-maps-scraper (see docs/RESOURCES.md).

Supplement only, never the primary source: scraping Maps sits in a ToS gray
area (docs/ROADMAP.md risks). Use it for areas/categories where OSM data is thin,
sparingly, with concurrency pinned to 1.

The scraper is an external binary — grab a release from
https://github.com/gosom/google-maps-scraper/releases and put it on PATH
(or set maps.binary in config.yaml).
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from pipeline.collectors.base import register
from pipeline.dedupe import normalize_domain
from pipeline.schema import Lead

# the scraper sometimes pulls an email out of a "//foo@bar.com" JS comment
# line in raw page source, leading-slash and all
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def build_queries(location: str, categories: list[str]) -> str:
    """One query per line; the "#!#<category>" custom-ID suffix comes back
    in the results' input_id column, carrying our category through."""
    return "\n".join(
        f"{category.strip()} in {location} #!#{category.strip()}"
        for category in categories
    )


def _split_emails(raw: str) -> list[str]:
    # the emails column is only filled with -email; seen as comma-joined,
    # sometimes wrapped in brackets/quotes depending on version
    cleaned = raw.strip().strip("[]")
    seen: dict[str, str] = {}
    for part in cleaned.split(","):
        email = part.strip().strip("'\"").lstrip("/")
        if email and _EMAIL_RE.fullmatch(email):
            seen.setdefault(email.lower(), email)
    return list(seen.values())


def parse_results_csv(path: str | Path, categories: set[str]) -> list[Lead]:
    leads: list[Lead] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            company = (row.get("title") or "").strip()
            if not company:
                continue
            input_id = (row.get("input_id") or "").strip()
            industry = input_id if input_id in categories else "other"
            leads.append(
                Lead(
                    company=company,
                    domain=normalize_domain(row.get("website")),
                    source="maps",
                    industry=industry,
                    contact_emails=_split_emails(row.get("emails") or ""),
                    contact_phones=(
                        [phone] if (phone := (row.get("phone") or "").strip()) else []
                    ),
                )
            )
    return leads


@register("maps")
class MapsCollector:
    def __init__(self, config: dict):
        maps = config.get("maps", {})
        self.binary: str = maps.get("binary", "google-maps-scraper")
        self.depth: int = maps.get("depth", 3)
        self.language: str = maps.get("language", "")
        self.extract_emails: bool = maps.get("extract_emails", True)
        self.timeout_s: int = maps.get("timeout_s", 900)

    def collect(self, params: dict) -> list[Lead]:
        location = params.get("location")
        categories = params.get("categories")
        if not location or not categories:
            raise ValueError(
                "maps collector needs a location and at least one category "
                "(--location / --categories, or collect.* in config.yaml)"
            )
        binary = shutil.which(self.binary)
        if not binary:
            raise RuntimeError(
                f"maps scraper binary {self.binary!r} not found on PATH — "
                "install a release from "
                "https://github.com/gosom/google-maps-scraper/releases "
                "or set maps.binary in config.yaml (see docs/RESOURCES.md)"
            )

        with tempfile.TemporaryDirectory(prefix="maps-collect-") as tmp:
            queries_path = Path(tmp) / "queries.txt"
            results_path = Path(tmp) / "results.csv"
            queries_path.write_text(
                build_queries(location, categories) + "\n", encoding="utf-8"
            )
            cmd = [
                binary,
                "-input", str(queries_path),
                "-results", str(results_path),
                "-depth", str(self.depth),
                "-c", "1",  # sparingly: single-threaded, same as Overpass
                "-exit-on-inactivity", "3m",
            ]
            if self.extract_emails:
                cmd.append("-email")
            if self.language:
                cmd += ["-lang", self.language]
            # the scraper's bundled playwright-go points at the retired
            # playwright.azureedge.net CDN for its first-run driver/browser
            # downloads, and cdn.playwright.dev rejects requests from here
            # (GatewayExceptionResponse) — the npmmirror mirror serves the
            # same build paths and works (verified 2026-07)
            env = os.environ.copy()
            env.setdefault(
                "PLAYWRIGHT_DOWNLOAD_HOST",
                "https://cdn.npmmirror.com/binaries/playwright",
            )
            try:
                subprocess.run(
                    cmd, check=True, capture_output=True, timeout=self.timeout_s, env=env
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"maps scraper timed out after {self.timeout_s}s — lower "
                    "maps.depth or query fewer categories per run"
                ) from None
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or b"").decode(errors="replace").strip()
                raise RuntimeError(f"maps scraper failed: {stderr or e}") from None
            if not results_path.exists():
                raise RuntimeError("maps scraper produced no results file")
            return parse_results_csv(results_path, {c.strip() for c in categories})
