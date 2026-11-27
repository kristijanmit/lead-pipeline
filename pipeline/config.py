"""Load and validate config.yaml — fail fast with a clear message at startup,
not three stages later as a cryptic KeyError (docs/ARCHITECTURE.md §10).

config.yaml holds defaults; CLI args override the values at runtime.
Secrets never live here — they go in the gitignored .env.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from pipeline.collectors.areas import parse_bbox
from pipeline.market import Market


class ConfigError(Exception):
    pass


DEFAULTS: dict = {
    # No niches and no default country here (docs/plan/v2 K1/K2): every run picks a
    # market preset and creates its niche in `go` (or --market / --niche-file).
    "collect": {
        "location": "",
        "manual_csv": "leads_manual.csv",
        # keys disqualified by L0 gates (written by score from v2 step 5); collect
        # skips them like seen_domains.json — separate because every seen entry
        # needs a Notion page_id
        "rejected_path": "data/rejected_domains.json",
    },
    # Market presets — facts about a country, not business choices. Validated
    # below; config.yaml normally defines them (serbia, uae).
    "markets": {},
    "overpass": {
        "endpoint": "https://overpass-api.de/api/interpreter",
        "timeout_s": 60,
    },
    "maps": {
        "binary": "google-maps-scraper",
        "depth": 3,
        "extract_emails": True,
        "timeout_s": 900,
        "grid_cell_km": 6,
        "timeout_per_cell_s": 60,
        "areas": {},
    },
    "enrich": {
        "delay_s": 2,
        "http_timeout_s": 20,
        "lighthouse": {
            "binary": "lighthouse",
            "timeout_s": 120,
        },
    },
    "draft": {
        "model": "haiku",
        "timeout_s": 120,
    },
    # Outreach copy rules (docs/OUTREACH.md). Link, named references and price are
    # OFF until the operator approves them here — the drafter never invents them.
    "outreach": {
        "link_in_first_message": False,
        "site_link": "onixweblab.com",
        "named_references": [],
        "price_from": "",
        "signature": "Kristijan, ONIX Web Lab, onixweblab.com",
        "examples_path": "config/outreach.md",
        # lowest Notion Tier drafting picks up (script_edits.md §11.2); a page with
        # no Tier (hand-made, backfilled) stays eligible
        "auto_draft_min_tier": "B",
    },
    "notion": {
        "database_id": "",
        "api_version": "2022-06-28",
        "delay_s": 0.35,
        "timeout_s": 30,
        "cache_path": "data/seen_domains.json",
    },
    # Scorer v4 (docs/plan/v2/PIPELINE.md §3). Per-market values (channel order,
    # review_strong, exclude_name_patterns) live on the market, not here.
    "scoring": {
        "version": 5,
        "gates": {
            "chain_listing_min": 3,
            "exclude_domains": [],
            "exclude_categories": [],
            "niche_mismatch": False,
        },
        "weights": {"need": 0.6, "viability": 0.4},
        "reach_by_rank": [100, 90, 80, 60, 40],
        "need": {
            "new_build": {"base": 60, "has_social": 25, "reviews_20plus": 15},
            "dead_site_base": 50,
            "redesign": {
                "not_mobile": 30,
                "no_https": 20,
                "outdated": 20,
                "perf_below_50": 15,
                "no_booking_or_call": 10,
                "seo_below_70": 5,
            },
            "outdated_years": 4,
            "legacy_cms": ["Joomla"],
        },
        "viability": {
            "no_data": 50,
            "no_reviews_quality": 20,
            "volume_at_strong": 40,
            "volume_max": 60,
            "rating_bands": [[4.5, 40], [4.0, 30], [3.5, 15]],
            "runs_ads": 10,
        },
        "tiers": {"A": 65, "B": 45, "C": 25},
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
    for removed in ("categories", "country"):
        if removed in collect:
            raise ConfigError(
                f"{path}: collect.{removed} was removed — the niche is created in `go` "
                "(or --niche-file) and the country comes from the market (markets:)"
            )
    if not isinstance(collect.get("rejected_path"), str) or not collect["rejected_path"]:
        raise ConfigError(f"{path}: collect.rejected_path must be a non-empty path")
    if "qualification" in config:
        raise ConfigError(
            f"{path}: qualification: was removed — the target country is the run's market (markets:)"
        )
    markets = config["markets"]
    if not isinstance(markets, dict):
        raise ConfigError(f"{path}: markets must be a mapping of id -> market preset")
    for market_id, preset in markets.items():
        if not isinstance(market_id, str) or market_id != market_id.strip().lower():
            raise ConfigError(f"{path}: markets: id {market_id!r} must be lowercase")
        try:
            Market.from_dict(market_id, preset)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{path}: markets.{market_id}: {e}") from None
    maps = config["maps"]
    if "language" in maps:
        raise ConfigError(
            f"{path}: maps.language was removed — set maps_language on the market (markets:)"
        )
    for key in ("depth", "timeout_s"):
        if not isinstance(maps.get(key), int) or maps[key] <= 0:
            raise ConfigError(f"{path}: maps.{key} must be a positive integer")
    for key in ("grid_cell_km", "timeout_per_cell_s"):
        if not isinstance(maps.get(key), (int, float)) or maps[key] <= 0:
            raise ConfigError(f"{path}: maps.{key} must be a positive number")
    if not isinstance(maps.get("areas"), dict):
        raise ConfigError(f"{path}: maps.areas must be a mapping of location -> bbox")
    for name, bbox in maps["areas"].items():
        try:
            parse_bbox(str(bbox))
        except ValueError as e:
            raise ConfigError(f"{path}: maps.areas.{name}: {e}") from None
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

    draft = config["draft"]
    if not isinstance(draft.get("model"), str) or not draft["model"]:
        raise ConfigError(f"{path}: draft.model must be a non-empty string")
    if not isinstance(draft.get("timeout_s"), int) or draft["timeout_s"] <= 0:
        raise ConfigError(f"{path}: draft.timeout_s must be a positive integer")

    outreach = config["outreach"]
    for key in ("link_in_first_message",):
        if not isinstance(outreach.get(key), bool):
            raise ConfigError(f"{path}: outreach.{key} must be true or false")
    for key in ("site_link", "price_from", "signature", "examples_path"):
        if not isinstance(outreach.get(key), str):
            raise ConfigError(f"{path}: outreach.{key} must be a string")
    if not isinstance(outreach.get("named_references"), list) or not all(
        isinstance(r, str) for r in outreach["named_references"]
    ):
        raise ConfigError(f"{path}: outreach.named_references must be a list of strings")
    if outreach.get("auto_draft_min_tier") not in ("A", "B", "C"):
        raise ConfigError(f"{path}: outreach.auto_draft_min_tier must be A, B or C")
    if "industry_labels" in outreach:
        raise ConfigError(
            f"{path}: outreach.industry_labels was removed — the draft phrase is the niche's "
            "`phrase`, created in `go`"
        )

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

    _validate_scoring(config["scoring"], path)


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_scoring(scoring: dict, path: Path) -> None:
    """Scorer v4 shape. A v3 block (reachability / opportunity) fails with a pointer."""
    if "reachability" in scoring or "opportunity" in scoring or scoring.get("version") not in (4, 5):
        raise ConfigError(
            f"{path}: scoring v4 expected (gates, weights, reach_by_rank, need, viability, tiers) — "
            "the v3 reachability/opportunity block was replaced, see docs/SCORING.md"
        )
    if scoring["version"] != 5:
        raise ConfigError(
            f"{path}: scoring.version must be 5 (v5 scores a Maps listing with no reviews yet "
            "via viability.no_reviews_quality, see docs/SCORING.md changelog)"
        )
    for section in ("gates", "weights", "need", "viability", "tiers"):
        if not isinstance(scoring.get(section), dict):
            raise ConfigError(f"{path}: scoring.{section} must be a mapping")
    gates = scoring["gates"]
    if not isinstance(gates.get("chain_listing_min"), int) or gates["chain_listing_min"] < 2:
        raise ConfigError(f"{path}: scoring.gates.chain_listing_min must be an integer >= 2")
    for key in ("exclude_domains", "exclude_categories"):
        if not isinstance(gates.get(key), list) or not all(isinstance(v, str) for v in gates[key]):
            raise ConfigError(f"{path}: scoring.gates.{key} must be a list of strings")
    if not isinstance(gates.get("niche_mismatch"), bool):
        raise ConfigError(f"{path}: scoring.gates.niche_mismatch must be true or false")

    weights = scoring["weights"]
    if set(weights) != {"need", "viability"} or not all(_num(v) and v >= 0 for v in weights.values()):
        raise ConfigError(f"{path}: scoring.weights must be need and viability, non-negative numbers")
    if abs(sum(weights.values()) - 1.0) > 0.001:
        raise ConfigError(f"{path}: scoring.weights must sum to 1.0")

    reach = scoring.get("reach_by_rank")
    if not isinstance(reach, list) or not reach or not all(_num(v) and 0 <= v <= 100 for v in reach):
        raise ConfigError(f"{path}: scoring.reach_by_rank must be a non-empty list of numbers 0-100")

    need = scoring["need"]
    new_build = need.get("new_build")
    if not isinstance(new_build, dict) or set(new_build) != {"base", "has_social", "reviews_20plus"}:
        raise ConfigError(f"{path}: scoring.need.new_build must have base, has_social, reviews_20plus")
    from pipeline.scoring.scorer import NEED_ITEMS

    redesign = need.get("redesign")
    if not isinstance(redesign, dict) or set(redesign) != set(NEED_ITEMS):
        raise ConfigError(f"{path}: scoring.need.redesign must have exactly {', '.join(NEED_ITEMS)}")
    points = {**{f"new_build.{k}": v for k, v in new_build.items()},
              **{f"redesign.{k}": v for k, v in redesign.items()},
              "dead_site_base": need.get("dead_site_base")}
    for key, value in points.items():
        if not _num(value) or not 0 <= value <= 100:
            raise ConfigError(f"{path}: scoring.need.{key} must be a number 0-100")
    if not isinstance(need.get("outdated_years"), int) or need["outdated_years"] <= 0:
        raise ConfigError(f"{path}: scoring.need.outdated_years must be a positive integer")
    if not isinstance(need.get("legacy_cms"), list) or not all(isinstance(v, str) for v in need["legacy_cms"]):
        raise ConfigError(f"{path}: scoring.need.legacy_cms must be a list of strings")

    via = scoring["viability"]
    for key in ("no_data", "no_reviews_quality", "volume_at_strong", "volume_max", "runs_ads"):
        if not _num(via.get(key)) or not 0 <= via[key] <= 100:
            raise ConfigError(f"{path}: scoring.viability.{key} must be a number 0-100")
    bands = via.get("rating_bands")
    if (
        not isinstance(bands, list)
        or not all(isinstance(b, list) and len(b) == 2 and all(_num(x) for x in b) for b in bands)
        or [b[0] for b in bands] != sorted((b[0] for b in bands), reverse=True)
    ):
        raise ConfigError(
            f"{path}: scoring.viability.rating_bands must be [min_rating, points] pairs, highest rating first"
        )

    tiers = scoring["tiers"]
    if set(tiers) != {"A", "B", "C"} or not all(_num(v) for v in tiers.values()):
        raise ConfigError(f"{path}: scoring.tiers must give numeric A, B and C thresholds")
    if not tiers["A"] > tiers["B"] > tiers["C"] >= 0:
        raise ConfigError(f"{path}: scoring.tiers must descend: A > B > C >= 0")
