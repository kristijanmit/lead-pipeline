"""Website discovery — finds the site of a lead the collector listed without one.

"No website" drives the highest opportunity score, but Maps and OSM often just
lack the field. For leads with no domain this guesses likely domains from the
company name and accepts one only if the page proves it is the same business
(a known phone number on the page, or the full company name in its title or
headings). Pure guessing against the lead's own would-be domain: no search
engine is scraped. Runs before the contact enricher, which then fetches and
audits the discovered site like any other.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

import requests
from bs4 import BeautifulSoup

from pipeline.dedupe import normalize_domain, phone_key
from pipeline.enrichers.base import register
from pipeline.enrichers.contact import (
    ContactEnricher,
    RobotsDisallowedError,
    extract_phones,
)
from pipeline.schema import Lead

_TLDS = ("rs", "com", "co.rs")


def _ascii(text: str) -> str:
    # Serbian diacritics: đ has no decomposition, so map it first
    text = text.lower().replace("đ", "dj")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _words(company: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _ascii(company))


def candidate_domains(company: str) -> list[str]:
    """"Elemento Detailing" -> elementodetailing.rs, elemento-detailing.rs, ... Pure."""
    words = _words(company)
    if not words:
        return []
    slugs = ["".join(words)]
    if len(words) > 1:
        slugs.append("-".join(words))
    return [f"{slug}.{tld}" for slug in slugs for tld in _TLDS]


def proves_identity(html: str, company: str, phones: list[str]) -> bool:
    """A page is the lead's own site if it shows one of the lead's known phone
    numbers, or its title/headings contain the whole company name. Pure."""
    known = {phone_key(p) for p in phones if phone_key(p)}
    if known and any(phone_key(p) in known for p in extract_phones(html)):
        return True
    soup = BeautifulSoup(html, "html.parser")
    heads = [soup.title.string] if soup.title and soup.title.string else []
    heads += [t.get_text(" ", strip=True) for t in soup.find_all(["h1", "h2"])]
    haystack = " ".join(_words(" ".join(heads)))
    needle = " ".join(_words(company))
    return bool(needle) and needle in haystack


@register("discovery")
class DiscoveryEnricher:
    def __init__(self, config: dict):
        self._fetcher = ContactEnricher(config)

    def enrich(self, lead: Lead) -> Lead:
        if lead.domain:
            return lead
        for candidate in candidate_domains(lead.company):
            try:
                url, html = self._fetcher._fetch_homepage(candidate)
            except (RobotsDisallowedError, RuntimeError, requests.RequestException):
                continue  # unreachable guess is the common case, not an error
            if proves_identity(html, lead.company, lead.contact_phones):
                # a redirect to a different host (parked page, reseller) is not it
                final = normalize_domain(url)
                if final == candidate or final.endswith("." + candidate):
                    return replace(lead, domain=final)
        return lead
