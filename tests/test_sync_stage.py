"""cmd_sync file handling — only scored leads go out, successful creates
flip to status "synced" in scored.jsonl, and a re-run is a no-op thanks to
the status + cache checks. The sink itself is faked; its own behavior is
covered in test_notion_sink.py."""

import json

from pipeline.runner import main
from pipeline.schema import Lead, read_jsonl, write_jsonl
from pipeline.sinks.notion_sink import SyncReport


def _lead(company, domain, status="scored", **overrides):
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

    manifest = json.loads((run_dir / "manifest.json").read_text())
    counts = manifest["sync"]["counts"]
    assert counts == {
        "input": 4,
        "created": 1,
        "already_in_notion": 0,
        "already_synced": 1,
        "skipped_not_scored": 1,
        "beyond_limit": 0,
        "failed": 1,
    }
    out = capsys.readouterr().out
    assert "1 created" in out and "Fails (fail.rs): Notion API 500" in out


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
