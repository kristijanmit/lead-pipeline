"""cmd_score file handling — the invariants the scorer itself can't
enforce: leads enrichment never reached are passed through unscored, a
--limit dry-run never truncates scored.jsonl, and qualification config from
the country gate comes from the run's market."""

import json

from pipeline.runner import main
from pipeline.schema import Lead, read_jsonl, write_jsonl
from support import MARKETS_YAML, SERBIA_BLOCK


def _lead(company, domain, status="enriched", **overrides):
    defaults = dict(industry="dentist", country="Serbia")
    return Lead(
        company=company,
        domain=domain,
        source="osm",
        status=status,
        **{**defaults, **overrides},
    )


def _make_run(tmp_path, monkeypatch, leads, config_extra="", market=SERBIA_BLOCK):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML + config_extra)
    run_dir = tmp_path / "data" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "enriched.jsonl", leads)
    if market:  # collect writes the run's market block into the manifest
        (run_dir / "manifest.json").write_text(json.dumps({"run_id": "test-run", "market": market}))
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

    scored = score(lead, DEFAULTS["scoring"], {"target_countries": ["Serbia"]})
    assert scored.lead_type == "unclear"


def test_limit_dry_run_keeps_all_rows_untouched(tmp_path, monkeypatch):
    leads = [
        _lead("A", "a.rs", lighthouse={"performance": 40}),
        _lead("B", "b.rs", lighthouse={"performance": 50}),
        _lead("C", "c.rs", lighthouse={"performance": 60}),
    ]
    run_dir = _make_run(tmp_path, monkeypatch, leads)
    assert main(["score", "--run", "test-run"]) == 0
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["score"]["counts"]["scored"] == 3

    scored_path = run_dir / "scored.jsonl"
    b_before = next(r for r in read_jsonl(scored_path) if r.company == "B")

    # a --limit dry-run must not drop B and C from the file
    assert main(["score", "--run", "test-run", "--limit", "1"]) == 0
    after = {lead.company: lead for lead in read_jsonl(scored_path)}
    assert set(after) == {"A", "B", "C"}
    assert after["B"] == b_before  # previously scored row kept untouched

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["score"]["counts"]["scored"] == 1
    assert manifest["score"]["counts"]["beyond_limit"] == 2


def test_uncommon_industry_qualifies(tmp_path, monkeypatch):
    lead = _lead("Plumbing Co", "plumbingco.rs", industry="plumbing_shop", contact_emails=["a@b.rs"])
    run_dir = _make_run(tmp_path, monkeypatch, [lead])
    assert main(["score", "--run", "test-run"]) == 0
    scored = read_jsonl(run_dir / "scored.jsonl")[0]
    assert scored.qualified is True
    assert scored.industry == "plumbing_shop"


def test_country_gate_is_the_run_market(tmp_path, monkeypatch):
    lead = _lead("Out of scope", "oos.rs", country="Croatia", contact_emails=["a@b.rs"])
    run_dir = _make_run(tmp_path, monkeypatch, [lead])
    assert main(["score", "--run", "test-run"]) == 0
    scored = read_jsonl(run_dir / "scored.jsonl")[0]
    assert scored.qualified is False
    assert scored.total_score is None

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["score"]["counts"]["qualified"] == 0


def test_pre_v2_run_needs_market_flag(tmp_path, monkeypatch, capsys):
    lead = _lead("Old", "old.rs", contact_emails=["a@b.rs"])
    run_dir = _make_run(tmp_path, monkeypatch, [lead], market=None)
    assert main(["score", "--run", "test-run"]) == 1
    assert "pass --market" in capsys.readouterr().err
    assert main(["score", "--run", "test-run", "--market", "serbia"]) == 0
    assert read_jsonl(run_dir / "scored.jsonl")[0].qualified is True


def test_market_flag_cannot_contradict_the_manifest(tmp_path, monkeypatch, capsys):
    _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs", contact_emails=["a@b.rs"])])
    assert main(["score", "--run", "test-run", "--market", "uae"]) == 1
    assert "collected for market 'serbia'" in capsys.readouterr().err


def test_uae_run_gates_on_uae(tmp_path, monkeypatch):
    uae = {"id": "uae", "country": "United Arab Emirates", "dial_code": "+971",
           "mobile_prefixes": ["+9715"], "language": "en", "channels": ["Instagram", "Email"]}
    leads = [
        _lead("Dubai Dental", "dd.ae", country="United Arab Emirates", contact_emails=["a@dd.ae"]),
        _lead("Beograd Dent", "bd.rs", contact_emails=["a@bd.rs"]),
    ]
    run_dir = _make_run(tmp_path, monkeypatch, leads, market=uae)
    assert main(["score", "--run", "test-run"]) == 0
    by = {l.company: l.qualified for l in read_jsonl(run_dir / "scored.jsonl")}
    assert by == {"Dubai Dental": True, "Beograd Dent": False}
