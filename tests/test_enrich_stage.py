import json
from dataclasses import replace

from pipeline.dedupe import lead_key
from pipeline.enrichers.base import apply_enrichers
from pipeline.runner import main
from pipeline.schema import Lead, read_jsonl, write_jsonl


def _lead(company="A", domain="a.rs"):
    return Lead(company=company, domain=domain, source="osm")


class FlakyEnricher:
    """Fails N times, then succeeds — exercises the retry-once policy."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def enrich(self, lead: Lead) -> Lead:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("connection timed out")
        return replace(lead, contact_emails=[*lead.contact_emails, "info@a.rs"])


def test_transient_failure_retried_once_and_clean():
    flaky = FlakyEnricher(failures=1)
    enriched = apply_enrichers(_lead(), [("contact", flaky)], retry_delay_s=0)
    assert flaky.calls == 2
    assert enriched.status == "enriched"
    assert enriched.errors == []
    assert enriched.contact_emails == ["info@a.rs"]


def test_second_failure_recorded_and_next_step_still_runs():
    broken = FlakyEnricher(failures=2)
    working = FlakyEnricher(failures=0)
    enriched = apply_enrichers(
        _lead(), [("contact", broken), ("audit", working)], retry_delay_s=0
    )
    assert broken.calls == 2  # retried exactly once, then given up
    assert working.calls == 1  # later steps run despite the earlier failure
    assert enriched.status == "enrich_failed"
    assert enriched.errors == ["contact: connection timed out"]
    assert enriched.contact_emails == ["info@a.rs"]  # partial data kept


def test_original_lead_never_mutated():
    lead = _lead()
    apply_enrichers(lead, [("contact", FlakyEnricher(failures=0))], retry_delay_s=0)
    assert lead.status == "collected"
    assert lead.contact_emails == []


def test_lead_key_used_for_idempotent_skip():
    # what cmd_enrich does: previously-enriched leads are matched by lead_key
    done = replace(_lead(), status="enriched", contact_emails=["info@a.rs"])
    previous = {lead_key(done): done}
    pending = _lead()  # same domain, fresh from collected.jsonl
    assert lead_key(pending) in previous
    # a lead without a domain keys on the company name instead
    assert lead_key(_lead("No Web Bistro", ""))[0] == "company"


def _make_run(tmp_path, monkeypatch, leads, config_extra="enrich:\n  delay_s: 0\n"):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(config_extra)
    run_dir = tmp_path / "data" / "runs" / "test-run"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "collected.jsonl", leads)
    return run_dir


def test_zero_contact_leads_filtered_out(tmp_path, monkeypatch):
    # both leads have no domain, so contact/audit enrichers no-op — no
    # network calls needed to exercise the filter
    leads = [
        Lead(company="Has Email", domain="", source="osm", contact_emails=["x@y.com"]),
        Lead(company="No Contact At All", domain="", source="osm"),
    ]
    run_dir = _make_run(tmp_path, monkeypatch, leads)
    assert main(["enrich", "--run", "test-run"]) == 0

    enriched = {lead.company: lead for lead in read_jsonl(run_dir / "enriched.jsonl")}
    assert set(enriched) == {"Has Email"}

    filtered = {lead.company: lead for lead in read_jsonl(run_dir / "filtered.jsonl")}
    assert set(filtered) == {"No Contact At All"}
    assert filtered["No Contact At All"].status == "enriched"


def test_filtered_lead_not_reprocessed_on_rerun(tmp_path, monkeypatch):
    leads = [Lead(company="No Contact At All", domain="", source="osm")]
    run_dir = _make_run(tmp_path, monkeypatch, leads)
    assert main(["enrich", "--run", "test-run"]) == 0
    assert main(["enrich", "--run", "test-run"]) == 0

    filtered = read_jsonl(run_dir / "filtered.jsonl")
    assert len(filtered) == 1  # not duplicated by the second run

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["enrich"]["counts"]["already_filtered"] == 1
    assert manifest["enrich"]["counts"]["processed"] == 0


def test_limit_truncated_lead_not_filtered(tmp_path, monkeypatch):
    leads = [Lead(company="No Contact At All", domain="", source="osm")]
    run_dir = _make_run(tmp_path, monkeypatch, leads)
    assert main(["enrich", "--run", "test-run", "--limit", "0"]) == 0

    enriched = read_jsonl(run_dir / "enriched.jsonl")
    assert enriched[0].status == "collected"  # still pending, never filtered
    assert read_jsonl(run_dir / "filtered.jsonl") == []
