"""Domain normalization and lead dedup.

The dedup key is the normalized domain, computed here and only here
(ARCHITECTURE.md §9). Every stage that needs to compare leads by site
imports normalize_domain from this module.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlparse

from pipeline.schema import Lead


def normalize_domain(raw: str | None) -> str:
    """"https://www.Example.com/about/" -> "example.com". Empty in, empty out."""
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    # urlparse only fills netloc when a scheme separator is present
    if "://" not in raw:
        raw = "//" + raw
    host = urlparse(raw).netloc
    host = host.rpartition("@")[2].partition(":")[0]  # drop userinfo and port
    if host.startswith("www."):
        host = host[4:]
    return host.rstrip(".")


def dedupe_leads(leads: Iterable[Lead]) -> list[Lead]:
    """Drop duplicates, first occurrence wins.

    Leads with a domain dedupe on it. Leads without one (real businesses
    with no website are still outreach targets) fall back to the
    case-folded company name so they aren't all collapsed together.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Lead] = []
    for lead in leads:
        if lead.domain:
            key = ("domain", lead.domain)
        else:
            key = ("company", lead.company.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(lead)
    return out
