import pytest

from pipeline.collectors.manual import ManualCollector, read_manual_csv


def _write(tmp_path, content: str):
    path = tmp_path / "leads_manual.csv"
    path.write_text(content, encoding="utf-8")
    return path


def test_read_manual_csv(tmp_path):
    path = _write(
        tmp_path,
        "company,website,industry,emails,phone\n"
        "Studio Alfa,https://www.StudioAlfa.rs/,it_company,"
        "hello@studioalfa.rs; sales@studioalfa.rs,+381 60 111 2222\n"
        "Referral Bob,,,,\n"
        ",,,,\n",
    )
    leads = read_manual_csv(path)
    assert len(leads) == 2  # blank-company row skipped

    alfa = leads[0]
    assert alfa.company == "Studio Alfa"
    assert alfa.domain == "studioalfa.rs"  # normalized
    assert alfa.source == "manual"
    assert alfa.industry == "it_company"
    assert alfa.contact_emails == ["hello@studioalfa.rs", "sales@studioalfa.rs"]
    assert alfa.contact_phones == ["+381 60 111 2222"]

    bob = leads[1]
    assert bob.company == "Referral Bob"
    assert bob.domain == ""
    assert bob.industry == "other"
    assert bob.contact_emails == []
    assert bob.contact_phones == []


def test_missing_company_column_raises(tmp_path):
    path = _write(tmp_path, "name,website\nStudio Alfa,alfa.rs\n")
    with pytest.raises(ValueError, match="missing column"):
        read_manual_csv(path)


def test_excel_bom_tolerated(tmp_path):
    path = _write(tmp_path, "﻿company,website,industry,emails,phone\nA,a.com,,,\n")
    assert read_manual_csv(path)[0].company == "A"


def test_collector_missing_file_raises(tmp_path):
    collector = ManualCollector({"collect": {"manual_csv": str(tmp_path / "nope.csv")}})
    with pytest.raises(ValueError, match="not found"):
        collector.collect({})


def test_collector_input_param_overrides_config(tmp_path):
    path = _write(tmp_path, "company,website,industry,emails,phone\nA,a.com,,,\n")
    collector = ManualCollector({"collect": {"manual_csv": "does-not-exist.csv"}})
    leads = collector.collect({"input": str(path)})
    assert [lead.company for lead in leads] == ["A"]
