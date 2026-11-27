"""Shared audit core — one website audit used by the `audit` enricher, the
`audit-report` command (v2 step 8) and later the public scanner
(docs/plan/v2/strategy_gaps.md S16). Works on a domain, knows nothing about Lead.

Two parts, kept apart so one can't cost the other (script_edits.md G1/G2):
1. The probe: one GET for the homepage. Its HTML gives https, CMS and the
   signals (copyright year, viewport, booking, click-to-call, messaging links,
   ads tags) — no extra request. A failed probe is data: site_reachable=False
   plus the failure kind.
2. Lighthouse (local CLI, `npm install -g lighthouse`), only for a site that
   answered. A failure there keeps everything the probe found.

Parsing (extract_signals, detect_cms, parse_lighthouse_report) is pure and
fixture-tested.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, UnicodeDammit

USER_AGENT = "agency-lead-pipeline/0.1 (single-operator lead tool)"

# Lighthouse category key -> Lead.lighthouse key
CATEGORIES = {
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

# probe failure kinds (Lead.probe_error). Only dns / refused mean "nothing is
# there"; timeout / http_error / ssl / other mean "something answered or may".
PROBE_ERRORS = ("dns", "refused", "timeout", "http_error", "ssl", "other")
# when https and http fail differently, the more alive-looking kind wins:
# a cert error on https plus a refusal on http is a live site, not a dead one
_ALIVENESS = ("http_error", "ssl", "timeout", "other", "refused", "dns")

_DNS_MARKERS = (
    "nameresolutionerror", "name or service not known", "nodename nor servname",
    "getaddrinfo failed", "failed to resolve", "no address associated", "temporary failure in name resolution",
)

# --- signals ---------------------------------------------------------------

# text markers a copyright year sits next to (Serbian with and without diacritics)
_COPYRIGHT_MARKERS = re.compile(
    r"©|\bcopyright\b|all rights reserved|sva prava zadr[žz]ana", re.IGNORECASE
)
_YEAR = re.compile(r"\b(19[89]\d|20\d\d)\b")
_COPYRIGHT_WINDOW = 80  # chars either side of a marker

# known booking widgets / scheduling services, matched in the raw HTML
_BOOKING_MARKERS = (
    "calendly.com", "setmore.com", "simplybook", "booksy.com", "fresha.com", "treatwell",
    "acuityscheduling.com", "youcanbook.me", "timify.com", "okadoc.com", "practo.com",
)

_ADS_PATTERNS = {
    "meta_pixel": re.compile(r"connect\.facebook\.net/[^\"']*fbevents\.js|\bfbq\(", re.IGNORECASE),
    "google_ads": re.compile(r"\bAW-\d{6,}\b|googleadservices\.com|googleads\.g\.doubleclick\.net"),
    "gtm": re.compile(r"googletagmanager\.com/gtm\.js|\bGTM-[A-Z0-9]{4,}\b"),
}

_MESSAGING_HREF = re.compile(
    r"^(viber|whatsapp):|^https?://(www\.)?(wa\.me|api\.whatsapp\.com|web\.whatsapp\.com)/", re.IGNORECASE
)


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


def _copyright_year(text: str, current_year: int) -> int | None:
    years = []
    for match in _COPYRIGHT_MARKERS.finditer(text):
        start = max(0, match.start() - _COPYRIGHT_WINDOW)
        window = text[start : match.end() + _COPYRIGHT_WINDOW]
        years += [int(y) for y in _YEAR.findall(window) if int(y) <= current_year]
    return max(years) if years else None


def extract_signals(html: str, current_year: int) -> dict:
    """Signals from the probe's homepage HTML. Pure: the caller passes the year."""
    soup = BeautifulSoup(html, "html.parser")
    hrefs = [(a.get("href") or "").strip() for a in soup.find_all("a")]
    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    date_input = soup.find("input", attrs={"type": re.compile("^(date|datetime-local)$", re.I)})
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ")
    lowered = html.lower()
    return {
        "copyright_year": _copyright_year(text, current_year),
        "has_viewport_meta": viewport is not None,
        "has_booking": date_input is not None or any(m in lowered for m in _BOOKING_MARKERS),
        "has_click_to_call": any(h.lower().startswith("tel:") for h in hrefs),
        "has_messaging_link": any(_MESSAGING_HREF.search(h) for h in hrefs),
        "ads_tags": [name for name, pattern in _ADS_PATTERNS.items() if pattern.search(html)],
    }


# --- probe -----------------------------------------------------------------


class ProbeError(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


def classify_probe_error(error: Exception) -> str:
    """requests exception -> one of PROBE_ERRORS. Order matters: SSLError and
    ConnectTimeout are both ConnectionErrors."""
    if isinstance(error, requests.exceptions.SSLError):
        return "ssl"
    if isinstance(error, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(error, requests.exceptions.HTTPError):
        return "http_error"
    if isinstance(error, requests.exceptions.ConnectionError):
        message = str(error).lower()
        if any(m in message for m in _DNS_MARKERS):
            return "dns"
        if "refused" in message:
            return "refused"
    return "other"


def html_text(response) -> str:
    """Decode an HTML response. Without a charset in the Content-Type header,
    requests falls back to ISO-8859-1 and UTF-8 "ć" / "Š" turn into "Ä\x87" / "Å ";
    then the page's own <meta charset> (else a UTF-8-first guess) decides."""
    if "charset" in response.headers.get("Content-Type", "").lower():
        return response.text
    return UnicodeDammit(response.content, is_html=True).unicode_markup or ""


# a 429's Retry-After is honoured up to this many seconds (also the wait when it is
# missing or unreadable); then one retry, and a second 429 is recorded as a failure
MAX_RETRY_AFTER_S = 10.0


def retry_after_s(value: str | None, cap: float = MAX_RETRY_AFTER_S) -> float:
    """Seconds to wait from a Retry-After header (delta-seconds or an HTTP date),
    clamped to 0..cap. Missing or unreadable: cap."""
    if not value:
        return cap
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return cap
    return max(0.0, min(cap, seconds))


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


class SiteClient:
    """Every GET to a lead's own site during enrich (contact, discovery, the audit
    probe) goes through one client, so the site sees one polite visitor:
    - requests to the same host are spaced by `same_domain_delay_s`;
    - a 429 waits Retry-After (max MAX_RETRY_AFTER_S) and is retried once;
    - the homepage probe is cached per domain, so contact and the audit share one
      fetch (failures too: probe already tried https and http, with the 429 retry).
    cmd_enrich shares one client across its enrichers; on their own they make their own."""

    def __init__(
        self, timeout_s: float, same_domain_delay_s: float = 0.0, *,
        max_retry_after_s: float = MAX_RETRY_AFTER_S, sleep=time.sleep, clock=time.monotonic,
    ):
        self.timeout_s = timeout_s
        self.same_domain_delay_s = same_domain_delay_s
        self.max_retry_after_s = max_retry_after_s
        self._sleep, self._clock = sleep, clock
        self._last: dict[str, float] = {}
        self._homepages: dict[str, tuple[str, str, bool] | ProbeError] = {}

    @classmethod
    def from_config(cls, config: dict) -> SiteClient:
        enrich = config.get("enrich", {})
        return cls(enrich["http_timeout_s"], enrich.get("same_domain_delay_s", 0.0))

    def wait_for(self, url: str) -> None:
        """Sleep until `same_domain_delay_s` has passed since the last request to this host."""
        last = self._last.get(_host(url))
        if last is not None and self.same_domain_delay_s:
            remaining = self.same_domain_delay_s - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)

    def _send(self, url: str):
        self.wait_for(url)
        try:
            return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout_s)
        finally:
            self._last[_host(url)] = self._clock()

    def get(self, url: str):
        """GET with host spacing and one retry after a 429. The caller checks the status."""
        response = self._send(url)
        if getattr(response, "status_code", 200) == 429:
            headers = getattr(response, "headers", None) or {}
            self._sleep(retry_after_s(headers.get("Retry-After"), self.max_retry_after_s))
            response = self._send(url)
        return response

    def probe(self, domain: str) -> tuple[str, str, bool]:
        """The homepage, https first -> (url, html, https), cached per domain.
        Raises ProbeError with the most alive-looking failure kind across both schemes."""
        if domain not in self._homepages:
            try:
                self._homepages[domain] = self._probe(domain)
            except ProbeError as e:
                self._homepages[domain] = e
        cached = self._homepages[domain]
        if isinstance(cached, ProbeError):
            raise cached
        return cached

    def _probe(self, domain: str) -> tuple[str, str, bool]:
        failures: list[tuple[str, str]] = []
        for scheme, is_https in (("https", True), ("http", False)):
            url = f"{scheme}://{domain}/"
            try:
                response = self.get(url)
                response.raise_for_status()
                return str(response.url), html_text(response), is_https
            except requests.RequestException as e:
                failures.append((classify_probe_error(e), f"{scheme}: {e}"))
        kind, _ = min(failures, key=lambda f: _ALIVENESS.index(f[0]))
        raise ProbeError(kind, " | ".join(detail for _, detail in failures)[:300])


def probe(domain: str, timeout_s: float, client: SiteClient | None = None) -> tuple[str, str, bool]:
    """One GET for the working homepage URL, https first -> (url, html, https).
    https means "the https:// URL works". Raises ProbeError with the most
    alive-looking failure kind across both schemes. `client` shares spacing,
    the 429 retry and the homepage cache with the rest of enrich."""
    return (client or SiteClient(timeout_s)).probe(domain)


# --- Lighthouse ------------------------------------------------------------


def parse_lighthouse_report(report: dict) -> tuple[dict[str, int], bool | None]:
    """Report JSON -> ({category: 0-100 int}, mobile_friendly).

    Category scores are nullable in Lighthouse (a category can error out);
    null scores are simply omitted. mobile_friendly comes from the viewport
    audit — Lighthouse runs mobile emulation by default, and a page without
    a viewport meta tag renders desktop-sized on phones.
    """
    scores: dict[str, int] = {}
    for raw_key, key in CATEGORIES.items():
        score = (report.get("categories", {}).get(raw_key) or {}).get("score")
        if score is not None:
            scores[key] = round(score * 100)
    viewport = (report.get("audits", {}).get("viewport") or {}).get("score")
    mobile_friendly = None if viewport is None else bool(viewport)
    return scores, mobile_friendly


def run_lighthouse(url: str, binary_name: str, timeout_s: int) -> dict:
    binary = shutil.which(binary_name)
    if not binary:
        raise RuntimeError(
            f"lighthouse binary {binary_name!r} not found on PATH — "
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
        "--only-categories=" + ",".join(CATEGORIES),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"lighthouse timed out after {timeout_s}s on {url}") from None
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"lighthouse failed on {url}: {stderr.splitlines()[-1] if stderr else result.returncode}")
    try:
        return json.loads(result.stdout)
    except ValueError as e:
        raise RuntimeError(f"lighthouse produced unparseable JSON for {url}: {e}") from None


# --- the audit -------------------------------------------------------------


@dataclass
class AuditResult:
    site_reachable: bool
    url: str = ""
    probe_error: str | None = None  # one of PROBE_ERRORS when the probe failed
    probe_detail: str = ""
    https: bool | None = None
    cms: str | None = None
    signals: dict = field(default_factory=dict)  # extract_signals() output
    lighthouse: dict[str, int] | None = None
    mobile_friendly: bool | None = None
    lighthouse_error: str = ""


def audit_site(
    domain: str, *, http_timeout_s: float, lighthouse_binary: str, lighthouse_timeout_s: int, current_year: int,
    client: SiteClient | None = None,
) -> AuditResult:
    """Probe, then Lighthouse. Never raises for a dead site or a Lighthouse
    failure — both come back as fields. With `client`, a homepage contact already
    fetched is reused, and Lighthouse waits for the host spacing too."""
    try:
        url, html, https = probe(domain, http_timeout_s, client)
    except ProbeError as e:
        return AuditResult(site_reachable=False, probe_error=e.kind, probe_detail=e.detail)
    result = AuditResult(
        site_reachable=True, url=url, https=https, cms=detect_cms(html),
        signals=extract_signals(html, current_year),
    )
    if client is not None:
        client.wait_for(url)
    try:
        scores, mobile_friendly = parse_lighthouse_report(
            run_lighthouse(url, lighthouse_binary, lighthouse_timeout_s)
        )
    except Exception as e:  # one attempt: a retry doubles the worst case (~2 min) for little gain
        result.lighthouse_error = str(e)
        return result
    result.lighthouse = scores or None
    result.mobile_friendly = mobile_friendly
    return result
