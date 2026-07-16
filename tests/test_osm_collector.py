import json
from pathlib import Path

import pytest

from pipeline.collectors.osm import build_query, parse_elements, resolve_category

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def overpass_sample() -> dict:
    return json.loads((FIXTURES / "overpass_sample.json").read_text(encoding="utf-8"))


CATEGORIES_BY_TAG = {
    ("shop", "bakery"): "bakery",
    ("amenity", "cafe"): "cafe",
    ("amenity", "restaurant"): "restaurant",
}


def test_resolve_known_category():
    assert resolve_category("bakery") == ("shop", "bakery")


def test_resolve_raw_tag_passthrough():
    assert resolve_category("craft=roofer") == ("craft", "roofer")


def test_resolve_unknown_category_raises():
    with pytest.raises(ValueError, match="unknown category"):
        resolve_category("underwater-basket-weaving")


def test_build_query_contains_area_and_clauses():
    query = build_query("Berlin", [("shop", "bakery"), ("amenity", "cafe")], 60)
    # matches local, English, and international area names
    assert 'area["name"="Berlin"];' in query
    assert 'area["name:en"="Berlin"];' in query
    assert 'area["int_name"="Berlin"];' in query
    assert 'nwr["shop"="bakery"](area.searchArea);' in query
    assert 'nwr["amenity"="cafe"](area.searchArea);' in query
    assert "[timeout:60]" in query


def test_build_query_escapes_quotes_in_location():
    query = build_query('Ev"il', [("shop", "bakery")], 60)
    assert 'area["name"="Ev\\"il"]' in query


def test_parse_elements(overpass_sample):
    leads = parse_elements(overpass_sample, CATEGORIES_BY_TAG)

    by_company = {lead.company: lead for lead in leads}
    # unnamed POI skipped
    assert set(by_company) == {
        "Bäckerei Schmidt",
        "Café Luna",
        "No Web Bistro",
        "Bäckerei Schmidt Filiale 2",
    }

    schmidt = by_company["Bäckerei Schmidt"]
    assert schmidt.domain == "baeckerei-schmidt.de"  # normalized
    assert schmidt.industry == "bakery"
    assert schmidt.contact_phones == ["+49 30 1234567"]
    assert schmidt.source == "osm"
    assert schmidt.status == "collected"

    luna = by_company["Café Luna"]
    assert luna.domain == "cafeluna.example"  # contact:website fallback
    # same address under "email" and "contact:email" is kept once
    assert luna.contact_emails == ["Hello@cafeluna.example"]
    assert luna.industry == "cafe"

    bistro = by_company["No Web Bistro"]
    assert bistro.domain == ""  # no website is data, not a dropped lead
    assert bistro.contact_phones == ["+49 30 7654321"]
