"""cmd_score file handling — the two invariants the scorer itself can't
enforce: leads enrichment never reached are passed through unscored, and a
--limit dry-run never truncates scored.jsonl (which may hold hand-edited
intent_score values, the only hand-entered data in the pipeline)."""

import json
from dataclasses import replace

import pytest

from pipeline.runner import main
from pipeline.schema import Lead, read_jsonl, write_jsonl


def _lead(company, domain, status="enriched", **overrides):
    return Lead(
        company=company, domain=domain, source="osm", status=status, **overrides
    )


def _make_run(tmp_path, monkeypatch, leads):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")  # empty file -> config DEFAULTS
    run_dir = tmp_path / "data" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "enriched.jsonl", leads)
    return run_dir


def test_pending_leads_pass_through_unscored(tmp_path, monkeypatch, capsys):
    run_dir = _make_run(
        tmp_path,
        monkeypatch,
        [
            _lead("Done", "done.rs", lighthouse={"performance": 50}),
            # enrichment never reached this one (a limited enrich run) — it
            # has a domain but no audit data, and must not be scored as if
            # the domain were unreachable
            _lead("Pending", "pending.rs", status="collected"),
        ],
    )
    assert main(["score", "--run", "test-run"]) == 0
    scored = {lead.company: lead for lead in read_jsonl(run_dir / "scored.jsonl")}
    assert scored["Done"].status == "scored"
    assert scored["Done"].total_score is not None
    assert scored["Pending"].status == "collected"
    assert scored["Pending"].total_score is None
    assert scored["Pending"].lead_type is None
    assert "1 leads still pending enrichment" in capsys.readouterr().out


def test_enrich_failed_leads_are_scored():
    # unlike pending, a failed enrichment is real data: someone tried and
    # the site was down — that's the unreachable/unclear case by design
    lead = _lead("Failed", "failed.rs", status="enrich_failed")
    from pipeline.config import DEFAULTS
    from pipeline.scoring import score

    scored = score(lead, DEFAULTS["scoring"])
    assert scored.lead_type == "unclear"


def test_limit_rerun_keeps_all_rows_and_hand_edited_intent(tmp_path, monkeypatch):
    leads = [
        _lead("A", "a.rs", lighthouse={"performance": 40}),
        _lead("B", "b.rs", lighthouse={"performance": 50}),
        _lead("C", "c.rs", lighthouse={"performance": 60}),
    ]
    run_dir = _make_run(tmp_path, monkeypatch, leads)
    assert main(["score", "--run", "test-run"]) == 0

    # the manual workflow (SCORING.md §5): hand-edit intent in scored.jsonl
    scored_path = run_dir / "scored.jsonl"
    rows = read_jsonl(scored_path)
    write_jsonl(
        scored_path,
        [replace(r, intent_score=70.0) if r.company == "C" else r for r in rows],
    )
    c_before = next(r for r in read_jsonl(scored_path) if r.company == "C")

    # a --limit dry-run must not drop B and C from the file
    assert main(["score", "--run", "test-run", "--limit", "1"]) == 0
    after = {lead.company: lead for lead in read_jsonl(scored_path)}
    assert set(after) == {"A", "B", "C"}
    assert after["C"] == replace(c_before, intent_score=70.0)  # untouched
    assert after["B"].total_score is not None  # previous scored row kept

    # and a full re-run recomputes C's total from the carried intent
    assert main(["score", "--run", "test-run"]) == 0
    c_rescored = next(r for r in read_jsonl(scored_path) if r.company == "C")
    assert c_rescored.intent_score == 70.0
    # c_before was scored with intent 0, so the recomputed total gains 70 x 0.15
    assert c_rescored.total_score == pytest.approx(c_before.total_score + 10.5, abs=0.1)

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["score"]["counts"]["scored"] == 3
    assert manifest["score"]["counts"]["with_intent"] == 1
