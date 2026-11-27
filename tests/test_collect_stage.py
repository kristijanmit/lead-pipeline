"""cmd_collect source resilience — a source listed as a fallback/supplement
(e.g. --source osm,maps) should still contribute leads when another listed
source fails operationally, instead of the whole collect aborting."""

import json

from pipeline.runner import main
from pipeline.schema import Lead, read_jsonl
from support import MARKETS_YAML, NICHE_YAML


class FakeCollector:
    def __init__(self, leads=None, error=None):
        self.leads = leads or []
        self.error = error

    def collect(self, params):
        if self.error is not None:
            raise self.error
        return self.leads


def _setup(tmp_path, monkeypatch, collectors: dict):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    (tmp_path / "niche.yaml").write_text(NICHE_YAML)
    monkeypatch.setattr(
        "pipeline.runner.get_collector", lambda name, config: collectors[name]
    )
    return tmp_path


def test_one_source_failing_does_not_sink_the_others(tmp_path, monkeypatch, capsys):
    collectors = {
        "osm": FakeCollector(error=RuntimeError("504 Server Error: Gateway Timeout")),
        "maps": FakeCollector(leads=[Lead(company="A", domain="a.rs", source="maps")]),
    }
    _setup(tmp_path, monkeypatch, collectors)
    assert (
        main(
            [
                "collect",
                "--source",
                "osm,maps",
                "--location",
                "Novi Sad",
                "--market",
                "serbia",
                "--niche-file",
                "niche.yaml",
            ]
        )
        == 0
    )

    run_dirs = list((tmp_path / "data" / "runs").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    leads = read_jsonl(run_dir / "collected.jsonl")
    assert [lead.company for lead in leads] == ["A"]

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["errors_by_source"] == {
        "osm": "504 Server Error: Gateway Timeout"
    }
    assert manifest["counts"]["raw_by_source"] == {"osm": 0, "maps": 1}

    err = capsys.readouterr().err
    assert "warning: osm collector failed: 504 Server Error: Gateway Timeout" in err


def test_all_sources_failing_is_still_a_hard_error(tmp_path, monkeypatch, capsys):
    collectors = {
        "osm": FakeCollector(error=RuntimeError("504 Server Error: Gateway Timeout")),
        "maps": FakeCollector(error=RuntimeError("scraper binary not found")),
    }
    _setup(tmp_path, monkeypatch, collectors)
    assert (
        main(
            [
                "collect",
                "--source",
                "osm,maps",
                "--location",
                "Novi Sad",
                "--market",
                "serbia",
                "--niche-file",
                "niche.yaml",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "all collectors failed" in err
    assert "504 Server Error: Gateway Timeout" in err
    assert "scraper binary not found" in err


def test_misconfiguration_still_aborts_immediately(tmp_path, monkeypatch, capsys):
    # a ValueError (e.g. missing --location) is a usage bug, not a flaky
    # operational failure — no other source can compensate for it, so it
    # should still propagate rather than being treated as a per-source
    # warning
    collectors = {
        "osm": FakeCollector(error=ValueError("osm collector needs a location")),
        "maps": FakeCollector(leads=[Lead(company="A", domain="a.rs", source="maps")]),
    }
    _setup(tmp_path, monkeypatch, collectors)
    assert (
        main(["collect", "--source", "osm,maps", "--market", "serbia", "--niche-file", "niche.yaml"]) == 1
    )
    err = capsys.readouterr().err
    assert "osm collector needs a location" in err


def test_collect_stamps_market_and_niche_and_writes_manifest_blocks(tmp_path, monkeypatch):
    captured = {}

    class Recording(FakeCollector):
        def collect(self, params):
            captured[self.name] = params["categories"]
            return self.leads

    osm, maps = Recording(), Recording(leads=[
        Lead(company="A", domain="a.rs", source="maps", industry="dentist"),
        Lead(company="B", domain="", source="maps", industry="other", country="Croatia"),
    ])
    osm.name, maps.name = "osm", "maps"
    _setup(tmp_path, monkeypatch, {"osm": osm, "maps": maps})
    args = ["collect", "--source", "osm,maps", "--market", "serbia", "--niche-file", "niche.yaml",
            "--location", "Novi Sad"]
    assert main(args) == 0
    # Maps gets the free-text search terms, OSM the tags
    assert captured == {"osm": ["dentist"], "maps": ["dentist", "dental clinic"]}

    run_dir = next((tmp_path / "data" / "runs").iterdir())
    for lead in read_jsonl(run_dir / "collected.jsonl"):
        assert (lead.market, lead.language, lead.country) == ("serbia", "sr", "Serbia")
        assert (lead.industry, lead.niche, lead.niche_label) == (
            "dental clinic", "dental clinic", "stomatološke ordinacije")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["market"]["id"] == "serbia" and manifest["market"]["country"] == "Serbia"
    assert manifest["niche"]["phrase"] == "stomatološke ordinacije"
    assert manifest["counts"]["new_by_category"] == {"dentist": 1, "other": 1}


def test_collect_without_niche_or_market_fails(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, {"maps": FakeCollector()})
    assert main(["collect", "--source", "maps", "--market", "serbia", "--location", "X"]) == 1
    assert "no niche: run go, or pass --niche-file" in capsys.readouterr().err
    assert main(["collect", "--source", "maps", "--niche-file", "niche.yaml", "--location", "X"]) == 1
    assert "no market" in capsys.readouterr().err


def test_osm_without_osm_tags_fails_before_collecting(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, {"osm": FakeCollector(error=AssertionError("must not run"))})
    (tmp_path / "niche.yaml").write_text("name: x\nsearch_terms: [x]\nphrase: y\n")
    assert main(["collect", "--source", "osm", "--market", "serbia", "--niche-file", "niche.yaml"]) == 1
    assert "no osm_tags" in capsys.readouterr().err


def test_collect_passes_market_settings_to_collectors(tmp_path, monkeypatch):
    captured = {}

    class Recording(FakeCollector):
        def collect(self, params):
            captured.update(params)
            return [Lead(company="A", domain="a.ae", source="maps")]

    _setup(tmp_path, monkeypatch, {"maps": Recording()})
    assert main(["collect", "--source", "maps", "--market", "uae", "--niche-file", "niche.yaml",
                 "--location", "Dubai"]) == 0
    run_dir = next((tmp_path / "data" / "runs").iterdir())
    assert captured["maps_language"] == "en"
    assert captured["prefer_name_en"] is True
    assert captured["raw_dir"] == str(run_dir.relative_to(tmp_path))


def _collect_with_caches(tmp_path, monkeypatch, extra=()):
    leads = [
        Lead(company="Seen", domain="seen.rs", source="maps"),
        Lead(company="Rejected", domain="rejected.rs", source="maps"),
        Lead(company="No Site", domain="", source="maps"),
        Lead(company="Fresh", domain="fresh.rs", source="maps"),
    ]
    _setup(tmp_path, monkeypatch, {"maps": FakeCollector(leads=leads)})
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "seen_domains.json").write_text(json.dumps({"domain:seen.rs": {"page_id": "p1"}}))
    (data / "rejected_domains.json").write_text(json.dumps({
        "domain:rejected.rs": {"reason": "chain", "run_id": "r0", "rejected_at": "2026-11-27"},
        "company:no site": {"reason": "closed", "run_id": "r0", "rejected_at": "2026-11-27"},
    }))
    args = ["collect", "--source", "maps", "--market", "serbia", "--niche-file", "niche.yaml",
            "--location", "Novi Sad", *extra]
    assert main(args) == 0
    run_dir = max((data / "runs").iterdir())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    return [lead.company for lead in read_jsonl(run_dir / "collected.jsonl")], manifest["counts"]


def test_collect_skips_seen_and_rejected_keys(tmp_path, monkeypatch, capsys):
    companies, counts = _collect_with_caches(tmp_path, monkeypatch)
    assert companies == ["Fresh"]
    assert (counts["already_in_notion"], counts["already_rejected"]) == (1, 2)
    assert "2 rejected in an earlier run" in capsys.readouterr().out


def test_include_seen_keeps_rejected_too(tmp_path, monkeypatch):
    companies, counts = _collect_with_caches(tmp_path, monkeypatch, ["--include-seen"])
    assert companies == ["Seen", "Rejected", "No Site", "Fresh"]
    assert (counts["already_in_notion"], counts["already_rejected"]) == (0, 0)
