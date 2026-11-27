"""cmd_sync file handling — only scored leads go out, successful creates
flip to status "synced" in scored.jsonl, and a re-run is a no-op thanks to
the status + cache checks. The sink itself is faked; its own behavior is
covered in test_notion_sink.py."""

import json

from pipeline.runner import main
from pipeline.schema import Lead, read_jsonl, write_jsonl
from pipeline.sinks.notion_sink import RescoreReport, SyncReport


def _lead(company, domain, status="scored", **overrides):
    overrides.setdefault("qualified", True)
    overrides.setdefault("tier", "B")
    return Lead(
        company=company, domain=domain, source="osm", status=status, **overrides
    )


class FakeSink:
    """Records what write() was asked to push; every lead succeeds unless
    its domain is listed in fail_domains."""

    def __init__(self, fail_domains=()):
        self.fail_domains = fail_domains
        self.written = []

    def write(self, leads, limit=None):
        self.written.append(leads)
        report = SyncReport()
        for lead in leads:
            if limit is not None and len(report.created) >= limit:
                report.pending += 1
            elif lead.domain in self.fail_domains:
                report.failed.append((lead, "Notion API 500: boom"))
            else:
                report.created.append(lead)
        return report


def _make_run(tmp_path, monkeypatch, leads, sink):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")  # empty file -> config DEFAULTS
    run_dir = tmp_path / "data" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "scored.jsonl", leads)
    monkeypatch.setattr("pipeline.runner.get_sink", lambda name, config: sink)
    return run_dir


def test_sync_flips_only_created_leads(tmp_path, monkeypatch, capsys):
    sink = FakeSink(fail_domains=("fail.rs",))
    run_dir = _make_run(
        tmp_path,
        monkeypatch,
        [
            _lead("Ok", "ok.rs"),
            _lead("Fails", "fail.rs"),
            _lead("Pending", "pending.rs", status="collected"),
            _lead("Done", "done.rs", status="synced"),
            _lead("Gated", "gated.rs", status="disqualified", disqualify_reason="chain: 4 listings"),
        ],
        sink,
    )
    assert main(["sync", "--run", "test-run", "--sink", "notion"]) == 0

    # only scored leads reached the sink
    assert [lead.company for lead in sink.written[0]] == ["Ok", "Fails"]
    after = {lead.company: lead for lead in read_jsonl(run_dir / "scored.jsonl")}
    assert after["Ok"].status == "synced"
    assert after["Fails"].status == "scored"  # a re-run retries it
    assert after["Pending"].status == "collected"
    assert after["Done"].status == "synced"
    assert after["Gated"].status == "disqualified"  # L0 gate: never synced

    manifest = json.loads((run_dir / "manifest.json").read_text())
    counts = manifest["sync"]["counts"]
    assert counts == {
        "input": 5,
        "created": 1,
        "already_in_notion": 0,
        "already_synced": 1,
        "skipped_disqualified": 1,
        "skipped_below_min_tier": 0,
        "skipped_not_scored": 1,
        "beyond_limit": 0,
        "failed": 1,
    }
    out = capsys.readouterr().out
    assert "1 created" in out and "Fails (fail.rs): Notion API 500" in out
    assert "1 disqualified (not synced)" in out


def test_rerun_is_a_noop_once_synced(tmp_path, monkeypatch):
    sink = FakeSink()
    run_dir = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs")], sink)
    assert main(["sync", "--run", "test-run", "--sink", "notion"]) == 0
    assert main(["sync", "--run", "test-run", "--sink", "notion"]) == 0

    # second run had nothing with status "scored" left to push
    assert sink.written[1] == []
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["sync"]["counts"]["already_synced"] == 1
    assert manifest["sync"]["counts"]["created"] == 0


def test_limit_leaves_pending_rows_intact(tmp_path, monkeypatch):
    sink = FakeSink()
    run_dir = _make_run(
        tmp_path,
        monkeypatch,
        [_lead("A", "a.rs", reachability_score=70.0), _lead("B", "b.rs")],
        sink,
    )
    assert main(["sync", "--run", "test-run", "--sink", "notion", "--limit", "1"]) == 0

    after = {lead.company: lead for lead in read_jsonl(run_dir / "scored.jsonl")}
    assert after["A"].status == "synced"
    assert after["A"].reachability_score == 70.0  # scored value untouched by sync
    assert after["B"].status == "scored"  # beyond limit, still in the file
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["sync"]["counts"]["beyond_limit"] == 1


def test_cache_hits_flip_to_synced(tmp_path, monkeypatch):
    class CachedSink(FakeSink):
        def write(self, leads, limit=None):
            # everything is already in Notion (e.g. synced from another run)
            return SyncReport(cached=list(leads))

    sink = CachedSink()
    run_dir = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs")], sink)
    assert main(["sync", "--run", "test-run", "--sink", "notion"]) == 0
    after = read_jsonl(run_dir / "scored.jsonl")
    assert after[0].status == "synced"
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["sync"]["counts"]["already_in_notion"] == 1


def test_sync_requires_scored_file(tmp_path, monkeypatch, capsys):
    sink = FakeSink()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    (tmp_path / "data" / "runs" / "empty-run").mkdir(parents=True)
    monkeypatch.setattr("pipeline.runner.get_sink", lambda name, config: sink)
    assert main(["sync", "--run", "empty-run", "--sink", "notion"]) == 1
    assert "run score first" in capsys.readouterr().err


def test_min_tier_keeps_lower_tiers_and_unscored_rows_out(tmp_path, monkeypatch, capsys):
    sink = FakeSink()
    run_dir = _make_run(tmp_path, monkeypatch, [
        _lead("A", "a.rs", tier="A"),
        _lead("B", "b.rs", tier="B"),
        _lead("C", "c.rs", tier="C"),
        _lead("D", "d.rs", tier="D"),
        _lead("V3", "v3.rs", tier=None),  # scored before v4: no tier
        _lead("Unq", "unq.rs", tier="A", qualified=False),
    ], sink)
    assert main(["sync", "--run", "test-run", "--sink", "notion"]) == 0
    assert [l.company for l in sink.written[0]] == ["A", "B", "C"]  # default --min-tier C
    counts = json.loads((run_dir / "manifest.json").read_text())["sync"]["counts"]
    assert counts["skipped_below_min_tier"] == 3
    assert "3 below tier C or unscored by v4" in capsys.readouterr().out
    after = {l.company: l.status for l in read_jsonl(run_dir / "scored.jsonl")}
    assert after["D"] == "scored" and after["A"] == "synced"

    assert main(["sync", "--run", "test-run", "--sink", "notion", "--min-tier", "B"]) == 0
    assert [l.company for l in sink.written[1]] == []  # A, B already synced; C now below


class RescoreSink(FakeSink):
    def __init__(self, tmp_path, in_notion):
        super().__init__()
        self.cache_path = tmp_path / "seen_domains.json"
        self.cache_path.write_text(json.dumps({k: {"page_id": "p-" + k, "snapshot": {}} for k in in_notion}))
        self.in_notion = in_notion
        self.rescored = []

    def rescore(self, leads, limit=None):
        self.rescored.append(leads)
        report = RescoreReport()
        for lead in leads:
            key = f"domain:{lead.domain}"
            if key in self.in_notion:
                report.updated.append(lead)
                report.matched_keys.add(key)
            else:
                report.no_page.append(lead)
        return report


def test_rescore_one_run_takes_scored_disqualified_and_synced_v4_rows(tmp_path, monkeypatch, capsys):
    sink = RescoreSink(tmp_path, ["domain:a.rs", "domain:gated.rs"])
    run_dir = _make_run(tmp_path, monkeypatch, [
        _lead("A", "a.rs"),  # re-scored after its sync: status is back to "scored"
        _lead("Gated", "gated.rs", status="disqualified", qualified=False, tier=None),
        _lead("New", "new.rs"),
        _lead("Old", "old.rs", tier=None, scoring_version=3),
        _lead("Pending", "pending.rs", status="collected"),
    ], sink)
    assert main(["sync", "--run", "test-run", "--sink", "notion", "--rescore"]) == 0
    assert sink.written == []  # never creates pages
    assert [l.company for l in sink.rescored[0]] == ["A", "Gated", "New"]
    counts = json.loads((run_dir / "manifest.json").read_text())["rescore"]["counts"]
    assert counts == {"candidates": 3, "updated": 2, "no_page": 1, "scored_before_v4": 1,
                      "beyond_limit": 0, "failed": 0}
    out = capsys.readouterr().out
    assert "2 pages updated, 1 leads with no Notion page" in out
    assert "1 rows scored before v4 skipped" in out
    # JSONL is untouched by --rescore
    assert {l.company: l.status for l in read_jsonl(run_dir / "scored.jsonl")}["A"] == "scored"


def test_rescore_all_runs_uses_latest_row_and_reports_pages_without_source(tmp_path, monkeypatch, capsys):
    sink = RescoreSink(tmp_path, ["domain:a.rs", "domain:handmade.rs"])
    _make_run(tmp_path, monkeypatch, [_lead("A old", "a.rs", tier="C")], sink)
    newer = tmp_path / "data" / "runs" / "zz-newer"
    newer.mkdir()
    write_jsonl(newer / "scored.jsonl", [_lead("A new", "a.rs", tier="A")])
    assert main(["sync", "--sink", "notion", "--rescore"]) == 0
    assert [(l.company, l.tier) for l in sink.rescored[0]] == [("A new", "A")]
    out = capsys.readouterr().out
    assert "rescore (all runs): 1 pages updated" in out
    assert "1 Notion pages have no v4 JSONL row" in out
