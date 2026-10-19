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
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from pipeline.collectors.base import register
from pipeline.dedupe import split_website
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


_GRID_ID_RE = re.compile(r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def category_from_input_id(input_id: str) -> str:
    """Grid mode returns "<category>-<uuid>" as input_id (one per cell);
    plain mode returns the bare category."""
    return _GRID_ID_RE.sub("", input_id.strip())


def parse_bbox(raw: str) -> tuple[float, float, float, float]:
    """"minLat,minLon,maxLat,maxLon" -> floats, validated. Raises ValueError."""
    try:
        min_lat, min_lon, max_lat, max_lon = (float(x) for x in raw.split(","))
    except ValueError:
        raise ValueError(
            f"bbox {raw!r} must be 4 comma-separated numbers: minLat,minLon,maxLat,maxLon"
        ) from None
    if not (-90 <= min_lat < max_lat <= 90 and -180 <= min_lon < max_lon <= 180):
        raise ValueError(f"bbox {raw!r}: min must be below max (lat -90..90, lon -180..180)")
    return min_lat, min_lon, max_lat, max_lon


def grid_cell_count(bbox: tuple[float, float, float, float], cell_km: float) -> int:
    """Approximate number of cells the scraper will search (it never searches
    fewer than one). 1 degree of latitude ~ 111 km; longitude shrinks with latitude."""
    min_lat, min_lon, max_lat, max_lon = bbox
    height_km = (max_lat - min_lat) * 111.0
    width_km = (max_lon - min_lon) * 111.0 * math.cos(math.radians((min_lat + max_lat) / 2))
    return max(1, math.ceil(height_km / cell_km)) * max(1, math.ceil(width_km / cell_km))


def _ascii_key(text: str) -> str:
    text = text.strip().lower().replace("đ", "dj")
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()).strip()


def resolve_bbox(params: dict, areas: dict) -> str | None:
    """The bounding box for this run: an explicit params["bbox"] wins, then the
    configured area for the location; params["grid"] is False -> no grid."""
    if params.get("grid") is False:
        return None
    if params.get("bbox"):
        return params["bbox"]
    wanted = _ascii_key(params.get("location") or "")
    for name, bbox in (areas or {}).items():
        if _ascii_key(str(name)) == wanted:
            return bbox
    return None


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


def _parse_rating(raw: str | None) -> float | None:
    try:
        return float((raw or "").strip().replace(",", "."))
    except ValueError:
        return None


def _parse_review_count(raw: str | None) -> int | None:
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else None


def parse_results_csv(path: str | Path, categories: set[str]) -> list[Lead]:
    leads: list[Lead] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            company = (row.get("title") or "").strip()
            if not company:
                continue
            input_id = (row.get("input_id") or "").strip()
            category = category_from_input_id(input_id)
            industry = category if category in categories else "other"
            domain, socials = split_website(row.get("website"))
            leads.append(
                Lead(
                    company=company,
                    domain=domain,
                    social_links=socials,
                    source="maps",
                    industry=industry,
                    address=(row.get("address") or "").strip(),
                    rating=_parse_rating(row.get("review_rating")),
                    review_count=_parse_review_count(row.get("review_count")),
                    profile_url=(row.get("link") or "").strip(),
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
        self.grid_cell_km: float = maps.get("grid_cell_km", 6)
        self.timeout_per_cell_s: int = maps.get("timeout_per_cell_s", 60)
        self.areas: dict = maps.get("areas") or {}

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
            timeout_s = self.timeout_s
            bbox = resolve_bbox(params, self.areas)
            if bbox:
                # one search per grid cell: every cell gets its own top results,
                # so depth 1 still covers a whole city (docs/COLLECTION.md §3)
                cells = grid_cell_count(parse_bbox(bbox), self.grid_cell_km)
                cmd += ["-grid-bbox", bbox, "-grid-cell", str(self.grid_cell_km)]
                timeout_s += cells * len(categories) * self.timeout_per_cell_s
                print(
                    f"  maps grid: ~{cells} cells x {len(categories)} categor"
                    f"{'y' if len(categories) == 1 else 'ies'}, timeout {timeout_s}s"
                )
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
                completed = subprocess.run(
                    cmd, check=True, capture_output=True, timeout=timeout_s, env=env
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"maps scraper timed out after {timeout_s}s — query fewer categories "
                    "per run, use a smaller area or split the work with `batch`"
                ) from None
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or b"").decode(errors="replace").strip()
                raise RuntimeError(f"maps scraper failed: {stderr or e}") from None
            if not results_path.exists():
                raise RuntimeError("maps scraper produced no results file")
            leads = parse_results_csv(results_path, {c.strip() for c in categories})
            if not leads and b'"status":"failed"' in completed.stderr + completed.stdout:
                # exit code 0 even when every search job died (e.g. playwright
                # "target closed") — don't let that pass as "no businesses found"
                log = (completed.stderr + completed.stdout).decode(errors="replace")
                failed = [ln for ln in log.splitlines() if '"status":"failed"' in ln]
                raise RuntimeError(f"maps scraper search failed: {failed[-1][:300]}")
            return leads
