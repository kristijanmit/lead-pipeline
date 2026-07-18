"""Load and validate config.yaml — fail fast with a clear message at startup,
not three stages later as a cryptic KeyError (docs/ARCHITECTURE.md §10).

config.yaml holds defaults; CLI args override the values at runtime.
Secrets never live here — they go in the gitignored .env.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


DEFAULTS: dict = {
    "collect": {
        "location": "",
        "country": "Serbia",
        "categories": [],
        "manual_csv": "leads_manual.csv",
    },
    "overpass": {
        "endpoint": "https://overpass-api.de/api/interpreter",
        "timeout_s": 60,
    },
    "maps": {
        "binary": "google-maps-scraper",
        "depth": 3,
        "language": "",
        "extract_emails": True,
        "timeout_s": 900,
    },
    "enrich": {
        "delay_s": 2,
        "http_timeout_s": 20,
        "lighthouse": {
            "binary": "lighthouse",
            "timeout_s": 120,
        },
    },
    "notion": {
        "database_id": "",
        "api_version": "2022-06-28",
        "delay_s": 0.35,
        "timeout_s": 30,
        "cache_path": "data/seen_domains.json",
    },
    "scoring": {
        "version": 3,
        "reachability": {
            "has_email": 50,
            "has_phone": 40,
            "has_social": 10,
        },
        "opportunity": {
            "no_website_score": 90,
            "audited_score_cap": 85,
            "no_https_bonus": 10,
            "not_mobile_friendly_bonus": 10,
            "lighthouse_weights": {
                "performance": 0.40,
                "best_practices": 0.25,
                "accessibility": 0.20,
                "seo": 0.15,
            },
        },
    },
    "qualification": {
        "target_countries": ["Serbia"],
        "industry_map": {
            "clinic": ["dentist", "dental", "dental_clinic", "orthodontist", "clinic", "medical", "doctor", "physician"],
            "car_workshop": ["car_repair", "auto_repair", "car_workshop", "mechanic", "tire", "tire_shop", "car_service"],
            "workshop_repair": ["repair_shop", "workshop", "appliance_repair", "electronics_repair", "shoe_repair"],
            "construction": ["construction", "contractor", "builder", "general_contractor", "renovation"],
        },
    },
}


def load_config(path: str | Path = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"config file not found: {p} — run from the repo root or pass --config"
        )
    try:
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {p}: {e}") from e
    if not isinstance(loaded, dict):
        raise ConfigError(f"{p} must be a YAML mapping, got {type(loaded).__name__}")

    config = _merge(DEFAULTS, loaded)
    _validate(config, p)
    return config


def load_env(path: str | Path = ".env") -> dict:
    """Minimal .env reader — KEY=VALUE lines, # comments — so secrets stay
    out of config.yaml without a new dependency. Values already present in
    the real environment win over the file."""
    env: dict[str, str] = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    env.update(os.environ)
    return env


def _merge(defaults: dict, override: dict) -> dict:
    merged = dict(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _validate(config: dict, path: Path) -> None:
    overpass = config["overpass"]
    if not isinstance(overpass.get("endpoint"), str) or not overpass["endpoint"]:
        raise ConfigError(f"{path}: overpass.endpoint must be a non-empty URL")
    if not isinstance(overpass.get("timeout_s"), int) or overpass["timeout_s"] <= 0:
        raise ConfigError(f"{path}: overpass.timeout_s must be a positive integer")
    collect = config["collect"]
    if not isinstance(collect.get("categories"), list):
        raise ConfigError(f"{path}: collect.categories must be a list")
    if not isinstance(collect.get("country"), str) or not collect["country"]:
        raise ConfigError(f"{path}: collect.country must be a non-empty string")
    maps = config["maps"]
    for key in ("depth", "timeout_s"):
        if not isinstance(maps.get(key), int) or maps[key] <= 0:
            raise ConfigError(f"{path}: maps.{key} must be a positive integer")
    enrich = config["enrich"]
    if not isinstance(enrich.get("delay_s"), (int, float)) or enrich["delay_s"] < 0:
        raise ConfigError(f"{path}: enrich.delay_s must be a non-negative number")
    if not isinstance(enrich.get("http_timeout_s"), int) or enrich["http_timeout_s"] <= 0:
        raise ConfigError(f"{path}: enrich.http_timeout_s must be a positive integer")
    lighthouse = enrich.get("lighthouse")
    if not isinstance(lighthouse, dict):
        raise ConfigError(f"{path}: enrich.lighthouse must be a mapping")
    if not isinstance(lighthouse.get("binary"), str) or not lighthouse["binary"]:
        raise ConfigError(f"{path}: enrich.lighthouse.binary must be a non-empty string")
    if not isinstance(lighthouse.get("timeout_s"), int) or lighthouse["timeout_s"] <= 0:
        raise ConfigError(f"{path}: enrich.lighthouse.timeout_s must be a positive integer")

    notion = config["notion"]
    # database_id may legitimately be empty until Phase 4 setup — the sink
    # itself refuses to construct without one, with a pointer to the README
    if not isinstance(notion.get("database_id"), str):
        raise ConfigError(f"{path}: notion.database_id must be a string")
    if not isinstance(notion.get("api_version"), str) or not notion["api_version"]:
        raise ConfigError(f"{path}: notion.api_version must be a non-empty string")
    if not isinstance(notion.get("delay_s"), (int, float)) or notion["delay_s"] < 0:
        raise ConfigError(f"{path}: notion.delay_s must be a non-negative number")
    if not isinstance(notion.get("timeout_s"), int) or notion["timeout_s"] <= 0:
        raise ConfigError(f"{path}: notion.timeout_s must be a positive integer")
    if not isinstance(notion.get("cache_path"), str) or not notion["cache_path"]:
        raise ConfigError(f"{path}: notion.cache_path must be a non-empty path")

    scoring = config["scoring"]
    if not isinstance(scoring.get("version"), int) or scoring["version"] <= 0:
        raise ConfigError(f"{path}: scoring.version must be a positive integer")
    for section in ("reachability", "opportunity"):
        if not isinstance(scoring.get(section), dict):
            raise ConfigError(f"{path}: scoring.{section} must be a mapping")
    for key, value in scoring["reachability"].items():
        if not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"{path}: scoring.reachability.{key} must be a non-negative number")
    opportunity = scoring["opportunity"]
    for key, value in opportunity.items():
        if key == "lighthouse_weights":
            continue
        if not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(
                f"{path}: scoring.opportunity.{key} must be a non-negative number"
            )
    # a bad audited site must never outrank "no website at all" — the
    # strongest new-build signal in the batch (docs/SCORING.md §4.2)
    if opportunity["audited_score_cap"] >= opportunity["no_website_score"]:
        raise ConfigError(
            f"{path}: scoring.opportunity.audited_score_cap must be less than "
            "no_website_score"
        )
    # lighthouse_weights must sum to 1.0 — a tuning typo here would silently
    # inflate or deflate every opportunity score in the batch
    lighthouse_weights = opportunity.get("lighthouse_weights")
    if not isinstance(lighthouse_weights, dict) or not lighthouse_weights:
        raise ConfigError(f"{path}: scoring.opportunity.lighthouse_weights must be a non-empty mapping")
    for key, value in lighthouse_weights.items():
        if not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(
                f"{path}: scoring.opportunity.lighthouse_weights.{key} must be a non-negative number"
            )
    if abs(sum(lighthouse_weights.values()) - 1.0) > 0.001:
        raise ConfigError(f"{path}: scoring.opportunity.lighthouse_weights must sum to 1.0")

    qualification = config["qualification"]
    countries = qualification.get("target_countries")
    if not isinstance(countries, list) or not countries or not all(isinstance(c, str) and c for c in countries):
        raise ConfigError(f"{path}: qualification.target_countries must be a non-empty list of strings")
    industry_map = qualification.get("industry_map")
    if not isinstance(industry_map, dict) or not industry_map:
        raise ConfigError(f"{path}: qualification.industry_map must be a non-empty mapping")
    for category, synonyms in industry_map.items():
        if not isinstance(synonyms, list) or not all(isinstance(s, str) and s for s in synonyms):
            raise ConfigError(
                f"{path}: qualification.industry_map.{category} must be a list of strings"
            )
