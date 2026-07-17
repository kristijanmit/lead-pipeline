"""cmd_collect source resilience — a source listed as a fallback/supplement
(e.g. --source osm,maps) should still contribute leads when another listed
source fails operationally, instead of the whole collect aborting."""

import json

from pipeline.runner import main
from pipeline.schema import Lead, read_jsonl


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
    (tmp_path / "config.yaml").write_text("")  # empty file -> config DEFAULTS
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
                "--categories",
                "dentist",
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
                "--categories",
                "dentist",
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
        main(["collect", "--source", "osm,maps", "--categories", "dentist"]) == 1
    )
    err = capsys.readouterr().err
    assert "osm collector needs a location" in err
