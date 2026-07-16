from dataclasses import replace

from pipeline.dedupe import lead_key
from pipeline.enrichers.base import apply_enrichers
from pipeline.schema import Lead


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
