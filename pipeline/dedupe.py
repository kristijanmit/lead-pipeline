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


def phone_key(raw: str) -> str:
    """Dedup key for one phone number: last 8 digits (leading zeros stripped
    first), so local and international spellings of the same number —
    "021/452-333" vs "+381 21 452 333" — collapse without any country-code
    table. Formatting is never normalized for display, only for comparison.
    """
    digits = "".join(c for c in raw if c.isdigit()).lstrip("0")
    return digits[-8:]


def lead_key(lead: Lead) -> tuple[str, str]:
    """Identity key for one lead across stages and re-runs.

    Leads with a domain key on it. Leads without one (real businesses with
    no website are still outreach targets) fall back to the case-folded
    company name so they aren't all collapsed together.
    """
    if lead.domain:
        return ("domain", lead.domain)
    return ("company", lead.company.strip().lower())


def email_keys(lead: Lead) -> list[str]:
    """Secondary identity signal: the lead's own emails, lowercased.

    Only meaningful once a lead is enriched (collect-time leads rarely have
    emails yet), so unlike lead_key this isn't used for intra-run stage
    idempotency — it's for catching the same business synced under two
    different domain/company keys (e.g. once before its domain was known,
    once after).
    """
    return [email.strip().lower() for email in lead.contact_emails if email.strip()]


def dedupe_leads(leads: Iterable[Lead]) -> list[Lead]:
    """Drop duplicates by lead_key, first occurrence wins."""
    seen: set[tuple[str, str]] = set()
    out: list[Lead] = []
    for lead in leads:
        key = lead_key(lead)
        if key in seen:
            continue
        seen.add(key)
        out.append(lead)
    return out
