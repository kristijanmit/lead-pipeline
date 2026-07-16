"""Load and validate config.yaml — fail fast with a clear message at startup,
not three stages later as a cryptic KeyError (ARCHITECTURE.md §10).

config.yaml holds defaults; CLI args override the values at runtime.
Secrets never live here — they go in the gitignored .env.
"""

from __future__ import annotations

from pathlib import Path

import yaml


class ConfigError(Exception):
    pass


DEFAULTS: dict = {
    "collect": {
        "location": "",
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
    "scoring": {
        "version": 1,
        "icp_fit": {
            "has_email": 30,
            "has_phone": 25,
            "has_social": 15,
            "established_bonus": 15,
            "established_threshold": 2,
            "industry_match": 15,
        },
        "website_audit": {
            "no_website_score": 90,
            "unreachable_domain_score": 65,
            "no_https_bonus": 10,
            "not_mobile_friendly_bonus": 10,
            "lighthouse_weights": {
                "performance": 0.40,
                "best_practices": 0.25,
                "accessibility": 0.20,
                "seo": 0.15,
            },
        },
        "total": {
            "icp_fit": 0.35,
            "website_audit": 0.50,
            "intent": 0.15,
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

    scoring = config["scoring"]
    if not isinstance(scoring.get("version"), int) or scoring["version"] <= 0:
        raise ConfigError(f"{path}: scoring.version must be a positive integer")
    for section in ("icp_fit", "website_audit", "total"):
        if not isinstance(scoring.get(section), dict):
            raise ConfigError(f"{path}: scoring.{section} must be a mapping")
    for key, value in scoring["icp_fit"].items():
        if not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"{path}: scoring.icp_fit.{key} must be a non-negative number")
    audit = scoring["website_audit"]
    for key, value in audit.items():
        if key == "lighthouse_weights":
            continue
        if not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(
                f"{path}: scoring.website_audit.{key} must be a non-negative number"
            )
    # both weight groups must sum to 1.0 — a tuning typo here would silently
    # inflate or deflate every score in the batch, so fail loudly at startup
    for name, group in (
        ("website_audit.lighthouse_weights", audit.get("lighthouse_weights")),
        ("total", scoring["total"]),
    ):
        if not isinstance(group, dict) or not group:
            raise ConfigError(f"{path}: scoring.{name} must be a non-empty mapping")
        for key, value in group.items():
            if not isinstance(value, (int, float)) or value < 0:
                raise ConfigError(f"{path}: scoring.{name}.{key} must be a non-negative number")
        if abs(sum(group.values()) - 1.0) > 0.001:
            raise ConfigError(f"{path}: scoring.{name} weights must sum to 1.0")
