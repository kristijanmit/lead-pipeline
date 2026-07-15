"""OSM collector — wraps the Overpass API.

Overpass is shared public infrastructure with a shared rate limit: one
single-threaded request per run, with a timeout and a small backoff retry.
Never parallelize queries against it.
"""

from __future__ import annotations

import time
from typing import Iterable

import requests

from pipeline.collectors.base import register
from pipeline.dedupe import normalize_domain
from pipeline.schema import Lead

# Friendly category names -> the OSM tag that marks them. Anything not in
# this table can be passed as a raw "key=value" tag on the CLI.
CATEGORY_TAGS: dict[str, tuple[str, str]] = {
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "bar": ("amenity", "bar"),
    "fast_food": ("amenity", "fast_food"),
    "dentist": ("amenity", "dentist"),
    "doctor": ("amenity", "doctors"),
    "pharmacy": ("amenity", "pharmacy"),
    "veterinary": ("amenity", "veterinary"),
    "driving_school": ("amenity", "driving_school"),
    "gym": ("leisure", "fitness_centre"),
    "hotel": ("tourism", "hotel"),
    "guest_house": ("tourism", "guest_house"),
    "hairdresser": ("shop", "hairdresser"),
    "beauty": ("shop", "beauty"),
    "bakery": ("shop", "bakery"),
    "butcher": ("shop", "butcher"),
    "florist": ("shop", "florist"),
    "furniture": ("shop", "furniture"),
    "jewelry": ("shop", "jewelry"),
    "optician": ("shop", "optician"),
    "car_repair": ("shop", "car_repair"),
    "car_dealer": ("shop", "car"),
    "bicycle_shop": ("shop", "bicycle"),
    "travel_agency": ("shop", "travel_agency"),
    "plumber": ("craft", "plumber"),
    "electrician": ("craft", "electrician"),
    "carpenter": ("craft", "carpenter"),
    "painter": ("craft", "painter"),
    "photographer": ("craft", "photographer"),
    "lawyer": ("office", "lawyer"),
    "accountant": ("office", "accountant"),
    "architect": ("office", "architect"),
    "estate_agent": ("office", "estate_agent"),
    "insurance": ("office", "insurance"),
    "it_company": ("office", "it"),
}

_WEBSITE_TAGS = ("website", "contact:website", "url")
_EMAIL_TAGS = ("email", "contact:email")
_PHONE_TAGS = ("phone", "contact:phone")

_MAX_ATTEMPTS = 3
_BACKOFF_S = 5

# OSM usage policy asks clients to identify themselves; the default
# python-requests agent also gets 406'd by some Overpass instances.
_USER_AGENT = "onix-lead-pipeline/0.1 (single-operator lead tool)"


def resolve_category(category: str) -> tuple[str, str]:
    """"bakery" -> ("shop", "bakery"); raw "craft=roofer" passes through."""
    category = category.strip()
    if "=" in category:
        key, _, value = category.partition("=")
        return (key.strip(), value.strip())
    if category in CATEGORY_TAGS:
        return CATEGORY_TAGS[category]
    raise ValueError(
        f"unknown category {category!r} — use a raw OSM 'key=value' tag "
        f"or one of: {', '.join(sorted(CATEGORY_TAGS))}"
    )


def build_query(location: str, tag_pairs: Iterable[tuple[str, str]], timeout_s: int) -> str:
    escaped = location.replace("\\", "\\\\").replace('"', '\\"')
    # Primary "name" tags are in the local language/script ("Нови Сад",
    # "München") — also match the English and international names so the
    # CLI accepts the spelling the operator actually types.
    areas = "\n".join(
        f'  area["{tag}"="{escaped}"];' for tag in ("name", "name:en", "int_name")
    )
    clauses = "\n".join(
        f'  nwr["{key}"="{value}"](area.searchArea);' for key, value in tag_pairs
    )
    return (
        f"[out:json][timeout:{timeout_s}];\n"
        f"(\n{areas}\n)->.searchArea;\n"
        f"(\n{clauses}\n);\n"
        f"out center;"
    )


def parse_elements(data: dict, categories_by_tag: dict[tuple[str, str], str]) -> list[Lead]:
    """Map raw Overpass elements to Leads. Unnamed POIs are skipped; a
    missing website is data (empty domain), not a reason to drop the lead."""
    leads: list[Lead] = []
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        website = next((tags[t] for t in _WEBSITE_TAGS if tags.get(t)), "")
        # the same address often appears in both "email" and "contact:email"
        emails: dict[str, str] = {}
        for tag in _EMAIL_TAGS:
            if tags.get(tag):
                emails.setdefault(tags[tag].lower(), tags[tag])
        industry = next(
            (cat for (key, value), cat in categories_by_tag.items() if tags.get(key) == value),
            "other",
        )
        leads.append(
            Lead(
                company=name,
                domain=normalize_domain(website),
                source="osm",
                industry=industry,
                contact_emails=list(emails.values()),
                contact_phone=next((tags[t] for t in _PHONE_TAGS if tags.get(t)), None),
            )
        )
    return leads


@register("osm")
class OsmCollector:
    def __init__(self, config: dict):
        overpass = config.get("overpass", {})
        self.endpoint: str = overpass["endpoint"]
        self.timeout_s: int = overpass["timeout_s"]

    def collect(self, params: dict) -> list[Lead]:
        location = params.get("location")
        categories = params.get("categories")
        if not location or not categories:
            raise ValueError(
                "osm collector needs a location and at least one category "
                "(--location / --categories, or collect.* in config.yaml)"
            )
        categories_by_tag = {resolve_category(c): c.strip() for c in categories}
        query = build_query(location, categories_by_tag.keys(), self.timeout_s)
        return parse_elements(self._fetch(query), categories_by_tag)

    def _fetch(self, query: str) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = requests.post(
                    self.endpoint,
                    data={"data": query},
                    headers={"User-Agent": _USER_AGENT},
                    # Overpass enforces its own [timeout:]; give the HTTP
                    # layer a little headroom past it
                    timeout=self.timeout_s + 15,
                )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as e:
                last_error = e
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(_BACKOFF_S * attempt)
        raise RuntimeError(
            f"Overpass request failed after {_MAX_ATTEMPTS} attempts: {last_error}"
        )
