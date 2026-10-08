"""Audit enricher — local Lighthouse CLI run plus HTTPS/CMS checks.

Lighthouse is the same engine behind the PageSpeed API, run locally
(`npm install -g lighthouse`) — no key, no quota, and all four category
scores instead of performance only (docs/RESOURCES.md). A cheap reachability
probe runs first so a dead site costs one GET, not a 60-second Lighthouse
attempt. Report parsing is a pure function — fixture-testable.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import replace

import requests
from bs4 import BeautifulSoup

from pipeline.enrichers.base import register
from pipeline.dedupe import is_social_domain
from pipeline.schema import Lead

_USER_AGENT = "agency-lead-pipeline/0.1 (single-operator lead tool)"

# Lighthouse category key -> Lead.lighthouse key
_CATEGORIES = {
    "performance": "performance",
    "accessibility": "accessibility",
    "best-practices": "best_practices",
    "seo": "seo",
}

# checked in order; markers beat the generator meta since page builders
# (Elementor etc.) overwrite the generator on what is still a WordPress site
_CMS_HTML_MARKERS = (
    ("wp-content", "WordPress"),
    ("wp-includes", "WordPress"),
    ("cdn.shopify.com", "Shopify"),
    ("wixstatic.com", "Wix"),
    ("parastorage.com", "Wix"),
    ("squarespace", "Squarespace"),
    ("website-files.com", "Webflow"),
)
_CMS_GENERATOR_NAMES = {
    "wordpress": "WordPress",
    "joomla": "Joomla",
    "drupal": "Drupal",
    "typo3": "TYPO3",
    "wix": "Wix",
    "squarespace": "Squarespace",
    "shopify": "Shopify",
    "webflow": "Webflow",
}


def detect_cms(html: str) -> str | None:
    lowered = html.lower()
    for marker, name in _CMS_HTML_MARKERS:
        if marker in lowered:
            return name
    generator = BeautifulSoup(html, "html.parser").find(
        "meta", attrs={"name": re.compile("^generator$", re.I)}
    )
    content = (generator.get("content") or "").strip() if generator else ""
    if not content:
        return None
    for token, name in _CMS_GENERATOR_NAMES.items():
        if token in content.lower():
            return name
    return content  # an unrecognized generator string is still data


def parse_lighthouse_report(report: dict) -> tuple[dict[str, int], bool | None]:
    """Report JSON -> ({category: 0-100 int}, mobile_friendly).

    Category scores are nullable in Lighthouse (a category can error out);
    null scores are simply omitted. mobile_friendly comes from the viewport
    audit — Lighthouse runs mobile emulation by default, and a page without
    a viewport meta tag renders desktop-sized on phones.
    """
    scores: dict[str, int] = {}
    for raw_key, key in _CATEGORIES.items():
        score = (report.get("categories", {}).get(raw_key) or {}).get("score")
        if score is not None:
            scores[key] = round(score * 100)
    viewport = (report.get("audits", {}).get("viewport") or {}).get("score")
    mobile_friendly = None if viewport is None else bool(viewport)
    return scores, mobile_friendly


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
                for key in _CATEGORIES.values()
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


@register("audit")
class AuditEnricher:
    def __init__(self, config: dict):
        enrich = config.get("enrich", {})
        self.http_timeout_s: int = enrich["http_timeout_s"]
        lighthouse = enrich.get("lighthouse", {})
        self.binary: str = lighthouse["binary"]
        self.lighthouse_timeout_s: int = lighthouse["timeout_s"]

    def enrich(self, lead: Lead) -> Lead:
        # a Facebook/Instagram page says nothing about the business's own site
        if not lead.domain or is_social_domain(lead.domain):
            return replace(lead, audit_notes=build_audit_notes(lead))

        url, html, https = self._probe(lead.domain)
        scores, mobile_friendly = parse_lighthouse_report(self._run_lighthouse(url))
        enriched = replace(
            lead,
            https=https,
            cms=detect_cms(html),
            lighthouse=scores or None,
            mobile_friendly=mobile_friendly,
        )
        return replace(enriched, audit_notes=build_audit_notes(enriched))

    def _probe(self, domain: str) -> tuple[str, str, bool]:
        """One GET to find the working URL — a dead site fails here for the
        price of a request, not a Lighthouse run. https means "the https://
        URL works", tried first."""
        last_error: Exception | None = None
        for scheme, is_https in (("https", True), ("http", False)):
            url = f"{scheme}://{domain}/"
            try:
                response = requests.get(
                    url, headers={"User-Agent": _USER_AGENT}, timeout=self.http_timeout_s
                )
                response.raise_for_status()
                return str(response.url), response.text, is_https
            except requests.RequestException as e:
                last_error = e
        raise RuntimeError(f"site unreachable: {last_error}")

    def _run_lighthouse(self, url: str) -> dict:
        binary = shutil.which(self.binary)
        if not binary:
            raise RuntimeError(
                f"lighthouse binary {self.binary!r} not found on PATH — "
                "run `npm install -g lighthouse` (see docs/RESOURCES.md) "
                "or set enrich.lighthouse.binary in config.yaml"
            )
        cmd = [
            binary,
            url,
            "--output=json",
            "--output-path=stdout",
            "--quiet",
            "--chrome-flags=--headless=new",
            "--only-categories=" + ",".join(_CATEGORIES),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=self.lighthouse_timeout_s
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"lighthouse timed out after {self.lighthouse_timeout_s}s on {url}"
            ) from None
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"lighthouse failed on {url}: {stderr.splitlines()[-1] if stderr else result.returncode}")
        try:
            return json.loads(result.stdout)
        except ValueError as e:
            raise RuntimeError(f"lighthouse produced unparseable JSON for {url}: {e}") from None
