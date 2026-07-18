"""Contact enricher — emails, phone numbers, and social links from the
lead's website.

Fetches the homepage plus at most one contact page, robots.txt-checked
first (CLAUDE.md). Keeps every email and every phone number found — never
collapses to one — merged with whatever the collector already provided.
Parsing is pure functions over HTML so tests run on fixtures, no network.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse, unquote
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from pipeline.dedupe import normalize_domain, phone_key
from pipeline.enrichers.base import register
from pipeline.schema import Lead

_USER_AGENT = "agency-lead-pipeline/0.1 (single-operator lead tool)"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# asset filenames ("logo@2x.png") match the email shape; kill by "TLD"
_ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".css", ".js", ".ico", ".woff", ".woff2",
)
# placeholder/tracker domains that show up inside page source, not real inboxes
_EMAIL_DOMAIN_BLOCKLIST = (
    "example.com", "example.org", "yourdomain.com", "domain.com",
    "sentry.io", "wixpress.com", "sentry.wixpress.com",
)

# candidate runs of digits/formatting; validated by _plausible_phone below
_PHONE_RE = re.compile(r"\+?\d[\d\s/().\-]{5,}\d")
# DD/MM/YYYY, DD.MM.YYYY (optional space after a separator), YYYY-MM-DD —
# page text with dates near phone numbers otherwise passes as a "phone"
_DATE_LIKE_RE = re.compile(
    r"\d{1,2}[./]\d{1,2}[./]\s?(?:19|20)\d{2}|(?:19|20)\d{2}-\d{1,2}-\d{1,2}"
)

_SOCIAL_HOSTS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com",),
    "x": ("x.com", "twitter.com"),
}
_SOCIAL_SHARE_MARKERS = ("/sharer", "/share", "/intent", "share.php", "/plugins/")

_CONTACT_LINK_MARKERS = ("contact", "kontakt")


class RobotsDisallowedError(Exception):
    """robots.txt forbids fetching this page — data, not a failure to retry."""


def _merge_unique(
    existing: Iterable[str], found: Iterable[str], key: Callable[[str], str]
) -> list[str]:
    """First occurrence wins, collector-provided values first."""
    seen: dict[str, str] = {}
    for value in [*existing, *found]:
        seen.setdefault(key(value), value)
    return list(seen.values())


def extract_emails(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith("mailto:"):
            # mailto: may carry ?subject=... and comma-separated addresses
            for address in unquote(href[7:]).partition("?")[0].split(","):
                candidates.append(address.strip())
    # regex over both raw source (attributes, JSON-LD) and rendered text
    # (catches entity-obfuscated addresses the raw source hides)
    candidates += _EMAIL_RE.findall(html)
    candidates += _EMAIL_RE.findall(soup.get_text(" "))

    emails: dict[str, str] = {}
    for candidate in candidates:
        candidate = candidate.strip().strip(".")
        if not _EMAIL_RE.fullmatch(candidate):
            continue
        domain = candidate.rpartition("@")[2].lower()
        if domain.endswith(_ASSET_EXTENSIONS):
            continue
        if any(domain == b or domain.endswith("." + b) for b in _EMAIL_DOMAIN_BLOCKLIST):
            continue
        emails.setdefault(candidate.lower(), candidate)
    return list(emails.values())


def _plausible_phone(candidate: str) -> bool:
    if _DATE_LIKE_RE.search(candidate):
        return False
    digits = sum(c.isdigit() for c in candidate)
    stripped = candidate.lstrip("(").strip()
    return 8 <= digits <= 15 and stripped[:1] in ("+", "0")


def extract_phones(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    phones: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith("tel:"):
            # sites write tel:+381..., tel://+381..., even tel:// +381...
            number = unquote(href[4:]).lstrip("/ ").strip()
            if sum(c.isdigit() for c in number) >= 6:
                phones.setdefault(phone_key(number), number)
    # visible text only — raw HTML is full of phone-shaped IDs in scripts
    for candidate in _PHONE_RE.findall(soup.get_text(" ")):
        candidate = candidate.strip()
        if _plausible_phone(candidate):
            phones.setdefault(phone_key(candidate), candidate)
    return list(phones.values())


def extract_social_links(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    links: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        host = normalize_domain(href)
        path = urlparse(href if "://" in href else "//" + href).path.lower()
        if path in ("", "/") or any(m in path for m in _SOCIAL_SHARE_MARKERS):
            continue  # bare network homepage or a share widget, not a profile
        for network, hosts in _SOCIAL_HOSTS.items():
            if network not in links and any(
                host == h or host.endswith("." + h) for h in hosts
            ):
                links[network] = href
    return links


def find_contact_url(html: str, base_url: str) -> str | None:
    """First link whose href or text says contact/kontakt."""
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:")) or href.startswith("#"):
            continue
        haystack = f"{href} {anchor.get_text(' ')}".lower()
        if any(marker in haystack for marker in _CONTACT_LINK_MARKERS):
            return urljoin(base_url, href)
    return None


@register("contact")
class ContactEnricher:
    def __init__(self, config: dict):
        enrich = config.get("enrich", {})
        self.timeout_s: int = enrich["http_timeout_s"]
        self._robots: dict[str, RobotFileParser | None] = {}

    def enrich(self, lead: Lead) -> Lead:
        if not lead.domain:
            return lead  # no website is data, not an error

        try:
            page_url, html = self._fetch_homepage(lead.domain)
        except RobotsDisallowedError as e:
            return replace(lead, errors=[*lead.errors, f"contact: {e}"])

        pages = [html]
        errors = list(lead.errors)
        contact_url = find_contact_url(html, page_url)
        # stay on the lead's own site — nav "contact" links sometimes point elsewhere
        if contact_url and normalize_domain(contact_url) == lead.domain:
            try:
                _, contact_html = self._fetch(contact_url)
                pages.append(contact_html)
            except RobotsDisallowedError as e:
                errors.append(f"contact: {e}")
            except requests.RequestException as e:
                errors.append(f"contact: contact page fetch failed: {e}")

        emails: list[str] = []
        phones: list[str] = []
        socials: dict[str, str] = {}
        for page in pages:
            emails += extract_emails(page)
            phones += extract_phones(page)
            socials = {**extract_social_links(page), **socials}

        return replace(
            lead,
            contact_emails=_merge_unique(lead.contact_emails, emails, str.lower),
            contact_phones=_merge_unique(lead.contact_phones, phones, phone_key),
            social_links={**socials, **lead.social_links},
            errors=errors,
        )

    def _fetch_homepage(self, domain: str) -> tuple[str, str]:
        last_error: Exception | None = None
        for scheme in ("https", "http"):
            try:
                return self._fetch(f"{scheme}://{domain}/")
            except RobotsDisallowedError:
                raise
            except requests.RequestException as e:
                last_error = e
        raise RuntimeError(f"homepage unreachable: {last_error}")

    def _fetch(self, url: str) -> tuple[str, str]:
        """robots.txt-checked GET; returns (final URL after redirects, HTML)."""
        robots = self._robots_for(url)
        if robots is not None and not robots.can_fetch(_USER_AGENT, url):
            raise RobotsDisallowedError(f"robots.txt disallows {url}")
        response = requests.get(
            url, headers={"User-Agent": _USER_AGENT}, timeout=self.timeout_s
        )
        response.raise_for_status()
        return str(response.url), response.text

    def _robots_for(self, url: str) -> RobotFileParser | None:
        base = url.partition("://")[0] + "://" + normalize_domain(url)
        if base not in self._robots:
            parser: RobotFileParser | None = RobotFileParser()
            try:
                response = requests.get(
                    f"{base}/robots.txt",
                    headers={"User-Agent": _USER_AGENT},
                    timeout=self.timeout_s,
                )
                if response.status_code >= 400:
                    parser = None  # no robots.txt -> everything is allowed
                else:
                    parser.parse(response.text.splitlines())
            except requests.RequestException:
                parser = None  # unreachable robots.txt never blocks the real fetch
            self._robots[base] = parser
        return self._robots[base]
