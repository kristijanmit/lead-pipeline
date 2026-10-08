"""Domain normalization and lead dedup.

The dedup key is the normalized domain, computed here and only here
(docs/ARCHITECTURE.md §9). Every stage that needs to compare leads by site
imports normalize_domain from this module.
"""

from __future__ import annotations

from dataclasses import replace
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


SOCIAL_HOSTS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com",),
    "x": ("x.com", "twitter.com"),
}


def social_network(domain: str) -> str | None:
    """"facebook.com" / "m.facebook.com" -> "facebook"; None for a real site."""
    for network, hosts in SOCIAL_HOSTS.items():
        if any(domain == h or domain.endswith("." + h) for h in hosts):
            return network
    return None


def is_social_domain(domain: str) -> bool:
    return social_network(domain) is not None


def split_website(raw: str | None) -> tuple[str, dict[str, str]]:
    """Raw "website" value -> (domain, social_links).

    A social profile entered as the website is not the lead's own site: it
    would be audited as one and every Facebook-only lead would share the
    dedup key "facebook.com". It becomes a social link with an empty domain.
    """
    domain = normalize_domain(raw)
    network = social_network(domain)
    if network is None:
        return domain, {}
    url = (raw or "").strip()
    if "://" not in url:
        url = "https://" + url
    return "", {network: url}


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


def _merge_duplicate(kept: Lead, dup: Lead) -> Lead:
    """Fill what `kept` is missing from `dup` — the same business found by two
    sources (e.g. OSM has the phone, Maps has the rating) keeps both. Nothing
    `kept` already has is overwritten, except profile_url, where the Google
    Maps listing beats an OSM object page."""
    updates: dict = {}
    for name in ("address", "rating", "review_count", "profile_url"):
        if getattr(kept, name) in ("", None):
            updates[name] = getattr(dup, name)
    if dup.source == "maps" and dup.profile_url and kept.source != "maps":
        updates["profile_url"] = dup.profile_url
    emails = {e.lower() for e in kept.contact_emails}
    new_emails = [e for e in dup.contact_emails if e.lower() not in emails]
    phones = {phone_key(p) for p in kept.contact_phones}
    new_phones = [p for p in dup.contact_phones if phone_key(p) not in phones]
    if new_emails:
        updates["contact_emails"] = [*kept.contact_emails, *new_emails]
    if new_phones:
        updates["contact_phones"] = [*kept.contact_phones, *new_phones]
    if dup.social_links:
        updates["social_links"] = {**dup.social_links, **kept.social_links}
    return replace(kept, **updates) if updates else kept


def dedupe_leads(leads: Iterable[Lead]) -> list[Lead]:
    """Drop duplicates by lead_key. The first occurrence wins; fields it is
    missing are filled from later duplicates (_merge_duplicate)."""
    out: list[Lead] = []
    index: dict[tuple[str, str], int] = {}
    for lead in leads:
        key = lead_key(lead)
        if key in index:
            out[index[key]] = _merge_duplicate(out[index[key]], lead)
            continue
        index[key] = len(out)
        out.append(lead)
    return out
