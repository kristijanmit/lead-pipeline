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
    },
    "overpass": {
        "endpoint": "https://overpass-api.de/api/interpreter",
        "timeout_s": 60,
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
