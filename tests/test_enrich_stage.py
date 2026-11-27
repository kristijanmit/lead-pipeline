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


# --- enrich --only audit --rescan ---------------------------------------------

def _rescan_run(tmp_path, monkeypatch, leads):
    run_dir = _make_run(tmp_path, monkeypatch, [])
    write_jsonl(run_dir / "enriched.jsonl", leads)
    return run_dir


def _fake_audit(monkeypatch, results):
    """results: domain -> AuditResult; records the domains audited."""
    from pipeline.enrichers.audit import AuditEnricher

    audited = []

    def audit(self, domain):
        audited.append(domain)
        return results[domain]

    monkeypatch.setattr(AuditEnricher, "audit", audit)
    return audited


def _old(company, domain, **kw):
    """A v9-style enriched lead: contact data and Lighthouse, no signals."""
    kw.setdefault("lighthouse", {"performance": 30})
    return Lead(
        company=company, domain=domain, source="osm", status="enriched",
        contact_emails=[f"info@{domain}"] if domain else ["x@y.rs"], https=False, **kw,
    )


def test_rescan_refreshes_audit_and_keeps_contact_data(tmp_path, monkeypatch, capsys):
    from pipeline.audit import AuditResult

    run_dir = _rescan_run(tmp_path, monkeypatch, [
        _old("Live", "live.rs"),
        _old("Slow LH", "slow.rs"),
        _old("Dead", "dead.rs"),
        _old("No Site", ""),
        _old("Insta", "instagram.com"),
    ])
    (run_dir / "scored.jsonl").write_text("")
    signals = {"copyright_year": 2017, "has_viewport_meta": False, "has_booking": False,
               "has_click_to_call": True, "has_messaging_link": False, "ads_tags": ["gtm"]}
    audited = _fake_audit(monkeypatch, {
        "live.rs": AuditResult(site_reachable=True, https=True, signals=signals, lighthouse={"performance": 55}),
        "slow.rs": AuditResult(site_reachable=True, https=True, signals=signals, lighthouse_error="lighthouse timed out"),
        "dead.rs": AuditResult(site_reachable=False, probe_error="dns", probe_detail="Failed to resolve"),
    })
    assert main(["enrich", "--run", "test-run", "--only", "audit", "--rescan"]) == 0
    assert audited == ["live.rs", "slow.rs", "dead.rs"]  # no site / social page: not audited

    by = {l.company: l for l in read_jsonl(run_dir / "enriched.jsonl")}
    assert by["Live"].contact_emails == ["info@live.rs"] and by["Live"].status == "enriched"
    assert by["Live"].https is True and by["Live"].lighthouse == {"performance": 55}
    assert by["Live"].copyright_year == 2017 and by["Live"].ads_tags == ["gtm"]
    assert by["Slow LH"].lighthouse == {"performance": 30}  # Lighthouse failed: old scores kept
    assert by["Slow LH"].has_click_to_call is True
    assert by["Slow LH"].errors == ["audit: lighthouse timed out (previous scores kept)"]
    assert by["Dead"].site_reachable is False and by["Dead"].probe_error == "dns"
    assert by["No Site"].site_reachable is None

    rescan = json.loads((run_dir / "manifest.json").read_text())["enrich_rescan"]
    assert rescan["step"] == "audit"
    assert rescan["counts"] == {"input": 5, "rescanned": 3, "site_unreachable": 1,
                                "lighthouse_failed": 1, "not_audited": 2, "pending": 0}
    assert "run `score --run test-run` again" in capsys.readouterr().out


def test_rescan_replaces_old_audit_errors_keeps_others(tmp_path, monkeypatch):
    from pipeline.audit import AuditResult

    old = replace(_old("MIS", "mis.rs"), errors=[
        "contact: contact page fetch failed: 429 Client Error",
        "audit: site unreachable (http_error): https: 429 Client Error",
    ])
    run_dir = _rescan_run(tmp_path, monkeypatch, [old, replace(_old("Down", "down.rs"), errors=["audit: lighthouse timed out"])])
    _fake_audit(monkeypatch, {
        "mis.rs": AuditResult(site_reachable=True, https=True, lighthouse={"performance": 80}),
        "down.rs": AuditResult(site_reachable=False, probe_error="dns", probe_detail="Failed to resolve"),
    })
    assert main(["enrich", "--run", "test-run", "--only", "audit", "--rescan"]) == 0
    by = {l.company: l for l in read_jsonl(run_dir / "enriched.jsonl")}
    assert by["MIS"].errors == ["contact: contact page fetch failed: 429 Client Error"]  # 429 line gone
    assert by["Down"].errors == ["audit: site unreachable (dns): Failed to resolve"]  # replaced, not appended


def test_rescan_respects_limit(tmp_path, monkeypatch):
    from pipeline.audit import AuditResult

    run_dir = _rescan_run(tmp_path, monkeypatch, [_old("A", "a.rs"), _old("B", "b.rs")])
    audited = _fake_audit(monkeypatch, {"a.rs": AuditResult(site_reachable=True, signals={})})
    assert main(["enrich", "--run", "test-run", "--only", "audit", "--rescan", "--limit", "1"]) == 0
    assert audited == ["a.rs"]
    by = {l.company: l for l in read_jsonl(run_dir / "enriched.jsonl")}
    assert by["A"].site_reachable is True and by["B"].site_reachable is None
    assert json.loads((run_dir / "manifest.json").read_text())["enrich_rescan"]["counts"]["pending"] == 1


def test_only_and_rescan_go_together(tmp_path, monkeypatch, capsys):
    _rescan_run(tmp_path, monkeypatch, [_old("A", "a.rs")])
    assert main(["enrich", "--run", "test-run", "--only", "audit"]) != 0
    assert main(["enrich", "--run", "test-run", "--rescan"]) != 0
    assert "--only and --rescan go together" in capsys.readouterr().err


def test_enrich_manifest_counts_audit_outcomes(tmp_path, monkeypatch):
    from pipeline.audit import AuditResult

    leads = [Lead(company="Dead", domain="dead.rs", source="osm", contact_emails=["a@dead.rs"])]
    run_dir = _make_run(tmp_path, monkeypatch, leads)
    _fake_audit(monkeypatch, {"dead.rs": AuditResult(site_reachable=False, probe_error="refused")})
    from pipeline.enrichers import contact, discovery
    monkeypatch.setattr(contact.ContactEnricher, "enrich", lambda self, lead: lead)
    monkeypatch.setattr(discovery.DiscoveryEnricher, "enrich", lambda self, lead: lead)
    assert main(["enrich", "--run", "test-run"]) == 0
    counts = json.loads((run_dir / "manifest.json").read_text())["enrich"]["counts"]
    assert counts["site_unreachable"] == 1 and counts["lighthouse_failed"] == 0 and counts["failed"] == 0
