from pathlib import Path

import pytest

from pipeline.collectors.maps import MapsCollector, build_queries, parse_results_csv

FIXTURES = Path(__file__).parent / "fixtures"


def test_build_queries_with_category_ids():
    queries = build_queries("Novi Sad", ["dentist", "hairdresser"])
    assert queries.splitlines() == [
        "dentist in Novi Sad #!#dentist",
        "hairdresser in Novi Sad #!#hairdresser",
    ]


def test_parse_results_csv():
    leads = parse_results_csv(
        FIXTURES / "maps_sample.csv", {"dentist", "hairdresser"}
    )
    by_company = {lead.company: lead for lead in leads}
    # untitled row skipped
    assert set(by_company) == {
        "Dental Centar Novak",
        "Zubar Petrović",
        "Salon Nina",
        "Mystery Biz",
    }

    novak = by_company["Dental Centar Novak"]
    assert novak.source == "maps"
    assert novak.domain == "dentalnovak.rs"  # normalized
    assert novak.industry == "dentist"  # via #!# custom-ID round trip
    # comma-joined emails split and case-insensitively deduped
    assert novak.contact_emails == ["info@dentalnovak.rs", "office@dentalnovak.rs"]
    assert novak.contact_phones == ["+381 21 123 456"]

    nina = by_company["Salon Nina"]
    assert nina.industry == "hairdresser"
    # bracket/quote-wrapped emails column still parses
    assert nina.contact_emails == ["nina@salonnina.example"]

    # input_id that isn't one of our categories (scraper-generated UUID)
    assert by_company["Mystery Biz"].industry == "other"
    assert by_company["Zubar Petrović"].domain == ""


def test_missing_binary_raises_with_install_hint():
    collector = MapsCollector({"maps": {"binary": "definitely-not-installed-xyz"}})
    with pytest.raises(RuntimeError, match="releases"):
        collector.collect({"location": "Novi Sad", "categories": ["dentist"]})
