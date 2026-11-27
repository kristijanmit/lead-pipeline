"""Audit enricher — maps the shared audit core (pipeline/audit.py) onto a Lead.

Probe and Lighthouse are separate: a dead site costs one GET, and a Lighthouse
failure keeps https, CMS and the signals the probe found. Neither raises —
both are data (site_reachable / probe_error, and a line in Lead.errors), so
apply_enrichers never retries the audit and the lead ends up "enriched".
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from pipeline.audit import (  # re-exported: tests and older imports use these names
    CATEGORIES,
    AuditResult,
    SiteClient,
    audit_site,
    detect_cms,
    parse_lighthouse_report,
)
from pipeline.dedupe import is_social_domain
from pipeline.enrichers.base import register
from pipeline.schema import Lead

__all__ = ["AuditEnricher", "apply_audit", "build_audit_notes", "detect_cms", "parse_lighthouse_report"]


def build_audit_notes(lead: Lead) -> str:
    """One line for Notion's Audit Notes: contact-data completeness plus
    all four Lighthouse categories (ROADMAP Phase 2, item 3)."""
    parts = [
        f"{len(lead.contact_emails)} emails, "
        f"{len(lead.contact_phones)} phones, "
        f"{len(lead.social_links)} socials"
    ]
    if not lead.domain:
        parts.append("no website")
        return "; ".join(parts)
    if is_social_domain(lead.domain):
        parts.append("social page only, not audited")
        return "; ".join(parts)
    if lead.lighthouse:
        abbrev = {"performance": "perf", "accessibility": "a11y", "best_practices": "bp"}
        parts.append(
            "LH "
            + " / ".join(
                f"{abbrev.get(key, key)} {lead.lighthouse[key]}"
                for key in CATEGORIES.values()
                if key in lead.lighthouse
            )
        )
    else:
        parts.append("no Lighthouse data")
    if lead.https is not None:
        parts.append("https ok" if lead.https else "no https")
    if lead.mobile_friendly is False:
        parts.append("not mobile friendly")
    if lead.cms:
        parts.append(f"cms: {lead.cms}")
    return "; ".join(parts)


def apply_audit(lead: Lead, result: AuditResult, keep_lighthouse: bool = False) -> Lead:
    """A new Lead with the audit fields from `result`. keep_lighthouse (rescan):
    when Lighthouse failed this time, keep the scores from the previous audit."""
    signals = result.signals
    lighthouse_failed = result.site_reachable and bool(result.lighthouse_error)
    keep = keep_lighthouse and lighthouse_failed and lead.lighthouse is not None
    # a re-audit replaces the previous audit's lines (e.g. an old 429) instead of piling up
    errors = [e for e in lead.errors if not e.startswith("audit:")]
    if not result.site_reachable:
        errors.append(f"audit: site unreachable ({result.probe_error}): {result.probe_detail}")
    elif lighthouse_failed:
        note = " (previous scores kept)" if keep else ""
        errors.append(f"audit: {result.lighthouse_error}{note}")
    audited = replace(
        lead,
        site_reachable=result.site_reachable,
        probe_error=result.probe_error,
        https=result.https,
        cms=result.cms,
        lighthouse=lead.lighthouse if keep else result.lighthouse,
        mobile_friendly=lead.mobile_friendly if keep else result.mobile_friendly,
        copyright_year=signals.get("copyright_year"),
        has_viewport_meta=signals.get("has_viewport_meta"),
        has_booking=signals.get("has_booking"),
        has_click_to_call=signals.get("has_click_to_call"),
        has_messaging_link=signals.get("has_messaging_link"),
        ads_tags=signals.get("ads_tags"),
        errors=errors,
    )
    return replace(audited, audit_notes=build_audit_notes(audited))


@register("audit")
class AuditEnricher:
    def __init__(self, config: dict):
        enrich = config.get("enrich", {})
        self.http_timeout_s: int = enrich["http_timeout_s"]
        lighthouse = enrich.get("lighthouse", {})
        self.binary: str = lighthouse["binary"]
        self.lighthouse_timeout_s: int = lighthouse["timeout_s"]
        self.keep_lighthouse = False  # set by `enrich --rescan`
        self.site = SiteClient.from_config(config)  # cmd_enrich shares one with contact

    def enrich(self, lead: Lead) -> Lead:
        # a Facebook/Instagram page says nothing about the business's own site
        if not lead.domain or is_social_domain(lead.domain):
            return replace(lead, audit_notes=build_audit_notes(lead))
        result = self.audit(lead.domain)
        return apply_audit(lead, result, keep_lighthouse=self.keep_lighthouse)

    def audit(self, domain: str) -> AuditResult:
        return audit_site(
            domain,
            http_timeout_s=self.http_timeout_s,
            lighthouse_binary=self.binary,
            lighthouse_timeout_s=self.lighthouse_timeout_s,
            current_year=datetime.now().year,
            client=self.site,
        )
