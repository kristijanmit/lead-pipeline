from pathlib import Path

import pytest

from pipeline.collectors.maps import (
    MapsCollector,
    _split_emails,
    build_queries,
    parse_results_csv,
)

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
    assert novak.address == "Bulevar oslobođenja 1, Novi Sad"
    assert novak.rating == 4.9
    assert novak.review_count == 127
    assert novak.profile_url == "https://maps.google.com/?cid=101"

    nina = by_company["Salon Nina"]
    assert nina.industry == "hairdresser"
    # bracket/quote-wrapped emails column still parses
    assert nina.contact_emails == ["nina@salonnina.example"]

    # input_id that isn't one of our categories (scraper-generated UUID)
    assert by_company["Mystery Biz"].industry == "other"
    assert by_company["Zubar Petrović"].domain == ""


def test_split_emails_strips_leading_slashes_and_dedupes():
    # seen live on "In Dental": scraper pulled emails out of a "//foo@bar"
    # JS comment line, leaving a stray leading "//" that must not be kept
    # as a distinct address from the clean version
    emails = _split_emails(
        "//indentalinfo@gmail.com, //indental.ns@gmail.com, "
        "info@indental.rs, indentalinfo@gmail.com"
    )
    assert emails == ["indentalinfo@gmail.com", "indental.ns@gmail.com", "info@indental.rs"]


def test_missing_binary_raises_with_install_hint():
    collector = MapsCollector({"maps": {"binary": "definitely-not-installed-xyz"}})
    with pytest.raises(RuntimeError, match="releases"):
        collector.collect({"location": "Novi Sad", "categories": ["dentist"]})


def test_category_from_grid_input_id():
    from pipeline.collectors.maps import category_from_input_id

    uuid = "3c0cd2b1-0ccb-45e7-91d4-391d328c554a"
    assert category_from_input_id(f"detailing-{uuid}") == "detailing"
    assert category_from_input_id(f"craft=roofer-{uuid}") == "craft=roofer"
    assert category_from_input_id("detailing") == "detailing"  # plain mode


def test_parse_bbox_validates():
    import pytest
    from pipeline.collectors.maps import parse_bbox

    assert parse_bbox("44.7,20.3,44.9,20.6") == (44.7, 20.3, 44.9, 20.6)
    for bad in ("1,2,3", "a,b,c,d", "45,20,44,21"):
        with pytest.raises(ValueError):
            parse_bbox(bad)


def test_grid_cell_count_scales_with_cell_size():
    from pipeline.collectors.maps import grid_cell_count, parse_bbox

    bbox = parse_bbox("44.70,20.30,44.92,20.62")
    assert grid_cell_count(bbox, 6) == 25
    assert grid_cell_count(bbox, 12) < grid_cell_count(bbox, 6)
    assert grid_cell_count(parse_bbox("44.0,20.0,44.001,20.001"), 6) == 1


def test_resolve_bbox_precedence_and_name_matching():
    from pipeline.collectors.maps import resolve_bbox

    areas = {"Niš": "1,1,2,2", "Novi Sad": "3,3,4,4"}
    assert resolve_bbox({"location": "nis"}, areas) == "1,1,2,2"  # diacritics/case
    assert resolve_bbox({"location": "Novi  Sad"}, areas) == "3,3,4,4"
    assert resolve_bbox({"location": "Zemun"}, areas) is None
    assert resolve_bbox({"location": "nis", "bbox": "9,9,10,10"}, areas) == "9,9,10,10"
    assert resolve_bbox({"location": "nis", "grid": False}, areas) is None


def test_collect_adds_grid_flags_and_scales_timeout(tmp_path, monkeypatch):
    import subprocess

    from pipeline.collectors import maps as maps_mod

    captured = {}

    def fake_run(cmd, check, capture_output, timeout, env):
        captured.update(cmd=cmd, timeout=timeout)
        results = cmd[cmd.index("-results") + 1]
        open(results, "w", encoding="utf-8").write("input_id,title\ndetailing-3c0cd2b1-0ccb-45e7-91d4-391d328c554a,A Shop\n")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(maps_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(maps_mod.shutil, "which", lambda b: "/bin/scraper")
    config = {"maps": {
        "binary": "x", "timeout_s": 900, "grid_cell_km": 6, "timeout_per_cell_s": 60,
        "areas": {"Beograd": "44.70,20.30,44.92,20.62"},
    }}
    leads = maps_mod.MapsCollector(config).collect({"location": "Beograd", "categories": ["detailing"]})
    cmd = captured["cmd"]
    assert cmd[cmd.index("-grid-bbox") + 1] == "44.70,20.30,44.92,20.62"
    assert cmd[cmd.index("-grid-cell") + 1] == "6"
    assert captured["timeout"] == 900 + 25 * 60
    assert leads[0].industry == "detailing"

    captured.clear()
    maps_mod.MapsCollector(config).collect({"location": "Beograd", "categories": ["detailing"], "grid": False})
    assert "-grid-bbox" not in captured["cmd"] and captured["timeout"] == 900
