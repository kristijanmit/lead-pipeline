"""Niche definitions — what a run is looking for (docs/plan/v2, strategy_gaps.md K1).

There are no default niches: every run's niche is created in `go`, or given in
full with `--niche-file` / a batch entry's `niche:` block. Nothing in config or
code names a niche. The definition is written into the run manifest and its
name is stamped on every lead as `industry` (the Notion `Industry` select) and
`niche`, its draft phrase as `niche_label`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from pipeline.collectors.osm import resolve_category

_LISTS = ("search_terms", "osm_tags", "accepted_categories")
_STRINGS = ("name", "phrase", "question", "landing_url")


@dataclass
class Niche:
    name: str  # e.g. "dental clinic" — becomes Lead.industry / Notion Industry
    search_terms: list[str]  # Google Maps queries, e.g. ["dentist", "dental clinic"]
    phrase: str  # plural draft phrase in the market language, e.g. "stomatološke ordinacije"
    osm_tags: list[str] = field(default_factory=list)  # raw "key=value" or osm.CATEGORY_TAGS keys
    accepted_categories: list[str] = field(default_factory=list)  # Google categories; empty = no check
    question: str = ""  # niche question for drafts; empty = generic
    landing_url: str = ""  # used only if links are turned on

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Niche:
        if not isinstance(data, dict):
            raise ValueError("niche must be a mapping with name, search_terms and phrase")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"niche has unknown keys {sorted(unknown)}")
        values = dict(data)
        for key in _LISTS:  # a single string is accepted as a one-item list (YAML convenience)
            if isinstance(values.get(key), str):
                values[key] = [values[key]]
        for key in ("name", "search_terms", "phrase"):
            values.setdefault(key, [] if key == "search_terms" else "")
        niche = cls(**values)
        niche.validate()
        return niche

    def validated(self) -> Niche:
        self.validate()
        return self

    def validate(self) -> None:
        for key in _STRINGS:
            if not isinstance(getattr(self, key), str):
                raise ValueError(f"niche {key} must be a string")
        for key in _LISTS:
            value = getattr(self, key)
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ValueError(f"niche {key} must be a list of non-empty strings")
        missing = [
            k for k in ("name", "search_terms", "phrase")
            if not (getattr(self, k).strip() if isinstance(getattr(self, k), str) else getattr(self, k))
        ]
        if missing:
            raise ValueError(f"niche is missing {', '.join(missing)}")
        for tag in self.osm_tags:
            resolve_category(tag)  # ValueError names the bad tag and the known keys


def check_sources(niche: Niche, sources: list[str]) -> None:
    """OSM needs tags, not free-text search terms — fail before anything is collected."""
    if "osm" in sources and not niche.osm_tags:
        raise ValueError(
            f"niche {niche.name!r} has no osm_tags — source osm needs raw OSM tags "
            "(e.g. amenity=dentist) or known keys (e.g. dentist); add osm_tags to the niche"
        )


def load_niche_file(path: str | Path) -> Niche:
    p = Path(path)
    if not p.exists():
        raise ValueError(f"niche file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML in {p}: {e}") from e
    if isinstance(data, dict) and isinstance(data.get("niche"), dict):
        data = data["niche"]  # accept a top-level `niche:` block too
    try:
        return Niche.from_dict(data)
    except ValueError as e:
        raise ValueError(f"{p}: {e}") from None
