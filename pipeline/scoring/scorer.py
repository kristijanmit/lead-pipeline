"""Pure scoring function, v4 — score(lead, scoring, market, niche, year) -> Lead, no I/O.

Four levels (docs/plan/v2/PIPELINE.md §3); this module is L0 and L1:

- L0 gates: can we contact them at all? First matching reason disqualifies.
- L1: niche match, Need (how visibly their web presence would improve),
  Viability (active, can pay), Reach (best first-touch channel, the market's
  order) -> priority -> tier A-D, with the reasons behind every number.

Keep this module free of I/O, network calls, config loading and clocks
(docs/ARCHITECTURE.md §13): the scoring dict, Market, Niche and the current
year all arrive as plain arguments.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from typing import TYPE_CHECKING

from pipeline.channels import best_channel
from pipeline.dedupe import is_social_domain
from pipeline.market import Market
from pipeline.schema import Lead

if TYPE_CHECKING:
    from pipeline.niche import Niche

# Need items a niche may re-weight via need_overrides (e.g. accommodation:
# {no_booking_or_call: 0}, because a booking widget is the whole job there).
NEED_ITEMS = (
    "not_mobile", "no_https", "outdated", "perf_below_50", "no_booking_or_call", "seo_below_70",
)
# Need items that come from Lighthouse — left out when Lighthouse failed.
_LIGHTHOUSE_ITEMS = ("perf_below_50", "seo_below_70")

# probe_error kinds (pipeline/audit.py): dns / refused = nothing is there; ssl =
# the site answers but browsers show a warning; the rest may be a live site.
_DEAD_PROBE_ERRORS = ("dns", "refused")

TIERS = ("A", "B", "C", "D")


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


# --- L0 gates -------------------------------------------------------------------


def _name_pattern_hit(company: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if re.search(rf"(?<!\w){re.escape(pattern)}(?!\w)", company, re.IGNORECASE):
            return pattern
    return None


def gate(lead: Lead, scoring: dict, market: Market, niche_match: str, channel: str) -> str | None:
    """The first L0 reason that disqualifies the lead, or None."""
    gates = scoring["gates"]
    if _norm(lead.country) != _norm(market.country):
        return f"country: {lead.country or 'unknown'} is not {market.country}"
    status = _norm(lead.business_status)
    if "closed" in status:
        return "closed: temporarily" if "temporarily" in status else "closed: permanently"
    if lead.listing_count is not None and lead.listing_count >= gates["chain_listing_min"]:
        return f"chain: {lead.listing_count} listings"
    if lead.domain and _norm(lead.domain) in {_norm(d) for d in gates["exclude_domains"]}:
        return f"chain: excluded domain {lead.domain}"
    if lead.maps_category and _norm(lead.maps_category) in {_norm(c) for c in gates["exclude_categories"]}:
        return f"excluded category: {lead.maps_category}"
    if pattern := _name_pattern_hit(lead.company, market.exclude_name_patterns):
        return f"excluded name: {pattern}"
    if gates["niche_mismatch"] and niche_match == "mismatch":
        return f"niche mismatch: {lead.maps_category}"
    if not channel:
        return "no usable contact channel"
    return None


# --- L1 sub-scores ----------------------------------------------------------------


def niche_match(lead: Lead, niche: Niche | None) -> str:
    """match / unknown / mismatch: Maps category vs the niche's accepted categories."""
    accepted = {_norm(c) for c in (niche.accepted_categories if niche else [])}
    if not accepted or not lead.maps_category:
        return "unknown"
    return "match" if _norm(lead.maps_category) in accepted else "mismatch"


def audit_case(lead: Lead) -> str:
    """Which Need branch applies:
    no_website · dead_site (dns/refused) · cert_error (ssl) · probe_failed
    (timeout/http_error/other) · audited (Lighthouse data) · no_audit (site live
    or not re-checked since v11, Lighthouse failed)."""
    if not lead.domain or is_social_domain(lead.domain):
        return "no_website"
    if lead.site_reachable is False:
        if lead.probe_error in _DEAD_PROBE_ERRORS:
            return "dead_site"
        if lead.probe_error == "ssl":
            return "cert_error"
        return "probe_failed"
    if lead.lighthouse:
        return "audited"
    return "no_audit"


# audit_case -> (lead_type, Need confidence)
_CASES = {
    "no_website": ("new_build", "high"),
    "dead_site": ("new_build", "high"),
    # the site exists (drafting reads lead_type), but every visitor sees a browser
    # warning, so Need uses the dead-site formula
    "cert_error": ("redesign", "high"),
    "probe_failed": ("unclear", "low"),
    "audited": ("redesign", "high"),
    "no_audit": ("unclear", "low"),
}


def _has_ig_or_fb(lead: Lead) -> bool:
    return bool(lead.social_links.get("instagram") or lead.social_links.get("facebook"))


def _new_build_need(lead: Lead, base: float, cfg: dict) -> tuple[float, list[str]]:
    need, reasons = base, []
    if _has_ig_or_fb(lead):
        need += cfg["has_social"]
        reasons.append("already on Instagram/Facebook")
    if (lead.review_count or 0) >= 20:
        need += cfg["reviews_20plus"]
        reasons.append("20+ reviews")
    return need, reasons


def _redesign_need(lead: Lead, points: dict, cfg: dict, year: int, with_lighthouse: bool) -> tuple[float, list[str]]:
    hits: list[tuple[str, str]] = []  # (item, reason)
    mobile = lead.mobile_friendly if with_lighthouse and lead.mobile_friendly is not None else None
    if mobile is None and lead.has_viewport_meta is not None:
        mobile = lead.has_viewport_meta
    if mobile is False:
        hits.append(("not_mobile", "not mobile-friendly"))
    if lead.https is False:
        hits.append(("no_https", "no HTTPS"))
    legacy = {_norm(c) for c in cfg["legacy_cms"]}
    if lead.copyright_year is not None and lead.copyright_year <= year - cfg["outdated_years"]:
        hits.append(("outdated", f"outdated (© {lead.copyright_year})"))
    elif lead.cms and _norm(lead.cms) in legacy:
        hits.append(("outdated", f"outdated builder ({lead.cms})"))
    if lead.has_booking is False and lead.has_click_to_call is False and not lead.has_messaging_link:
        hits.append(("no_booking_or_call", "no booking, tap-to-call or messaging link"))
    if with_lighthouse and lead.lighthouse:
        if "performance" in lead.lighthouse and lead.lighthouse["performance"] < 50:
            hits.append(("perf_below_50", "slow site"))
        if "seo" in lead.lighthouse and lead.lighthouse["seo"] < 70:
            hits.append(("seo_below_70", "weak SEO basics"))
    need = 0.0
    reasons = []
    for item, reason in hits:
        if points[item] > 0:
            need += points[item]
            reasons.append(reason)
    return need, reasons


def need(lead: Lead, case: str, scoring: dict, niche: Niche | None, year: int) -> tuple[float, list[str]]:
    """0-100: problems an owner can see on their own phone, not Lighthouse numbers."""
    cfg = scoring["need"]
    points = {**cfg["redesign"], **(getattr(niche, "need_overrides", None) or {})}
    if case == "no_website":
        value, reasons = _new_build_need(lead, cfg["new_build"]["base"], cfg["new_build"])
        reasons.insert(0, "no website")
    elif case in ("dead_site", "cert_error", "probe_failed"):
        value, reasons = _new_build_need(lead, cfg["dead_site_base"], cfg["new_build"])
        head = {
            "dead_site": f"domain dead ({lead.probe_error})",
            "cert_error": "certificate error, browsers show a warning",
            "probe_failed": f"site didn't load ({lead.probe_error or 'unknown'})",
        }[case]
        reasons.insert(0, head)
    elif case == "audited":
        value, reasons = _redesign_need(lead, points, cfg, year, with_lighthouse=True)
    else:  # no_audit
        value, reasons = _redesign_need(lead, points, cfg, year, with_lighthouse=False)
        if lead.site_reachable is None:
            reasons.append("no audit signals (old run: enrich --only audit --rescan)")
    if not reasons:
        reasons.append("site looks fine")
    return min(100.0, value), reasons


def viability(lead: Lead, scoring: dict, market: Market) -> tuple[float, list[str]]:
    """0-100: active and able to pay. Volume reaches `volume_at_strong` at the
    market's review_strong count (Serbia 50, UAE 100), capped at volume_max."""
    cfg = scoring["viability"]
    reasons: list[str] = []
    if lead.rating is None and lead.review_count is None:
        value = float(cfg["no_data"])
        reasons.append(f"no rating data (viability {_fmt(value)})")
    else:
        reviews = lead.review_count or 0
        volume = cfg["volume_at_strong"] * math.log10(reviews + 1) / math.log10(market.review_strong + 1)
        volume = min(float(cfg["volume_max"]), volume)
        quality = 0.0
        if lead.rating is not None:
            for minimum, pts in cfg["rating_bands"]:
                if lead.rating >= minimum:
                    quality = float(pts)
                    break
        value = volume + quality
        if lead.rating is not None and lead.review_count is not None:
            reasons.append(f"{_fmt(lead.rating)}★/{lead.review_count} reviews")
        elif lead.rating is not None:
            reasons.append(f"{_fmt(lead.rating)}★")
        else:
            reasons.append(f"{lead.review_count} reviews")
    if lead.ads_tags:
        value += cfg["runs_ads"]
        reasons.append("runs ads")
    return min(100.0, value), reasons


def reach(channel: str, scoring: dict, market: Market) -> float:
    """Value of the best first-touch channel by its rank in the market's order;
    0 when there is none (excluded channels never are the best channel)."""
    if not channel:
        return 0.0
    values = scoring["reach_by_rank"]
    rank = market.channels.index(channel)
    return float(values[min(rank, len(values) - 1)])


def tier_for(priority: float, tiers: dict) -> str:
    for name in ("A", "B", "C"):
        if priority >= tiers[name]:
            return name
    return "D"


def _cap(tier: str, ceiling: str) -> str:
    return max(tier, ceiling, key=TIERS.index)


# --- audit notes (facts only, the draft prompt reads them) -----------------------


def _build_audit_notes(lead: Lead, case: str) -> str:
    """Facts for drafting: who they are, their web presence, how to reach them.
    No ratings, scores or Lighthouse numbers — those live in score_reasons,
    which drafting never sees (script_edits.md P5/G12)."""
    parts = [f"{lead.industry}, {lead.location + ', ' if lead.location else ''}{lead.country}."]
    if case == "no_website":
        parts.append("No website.")
    elif case == "dead_site":
        parts.append(f"Domain {lead.domain} does not load (nothing there).")
    elif case == "cert_error":
        parts.append(f"Website {lead.domain} shows a browser security warning (certificate error).")
    elif case == "probe_failed":
        parts.append(f"Website {lead.domain} did not load when checked; may be temporary.")
    else:
        facts = []
        if lead.https is False:
            facts.append("no HTTPS")
        if lead.mobile_friendly is False or (lead.mobile_friendly is None and lead.has_viewport_meta is False):
            facts.append("not mobile friendly")
        if lead.lighthouse and lead.lighthouse.get("performance", 100) < 50:
            facts.append("slow to load")
        if lead.copyright_year is not None:
            facts.append(f"copyright year {lead.copyright_year}")
        if lead.cms:
            facts.append(f"built on {lead.cms}")
        if lead.has_booking:
            facts.append("has online booking")
        if lead.has_booking is False and lead.has_click_to_call is False and not lead.has_messaging_link:
            facts.append("no online booking, tap-to-call or messaging link")
        tail = (": " + "; ".join(facts)) if facts else ""
        parts.append(f"Has a website ({lead.domain}){tail}.")
    found = [
        name for name, ok in (
            ("email", bool(lead.contact_emails)),
            ("phone", bool(lead.contact_phones)),
            ("Instagram", bool(lead.social_links.get("instagram"))),
            ("Facebook", bool(lead.social_links.get("facebook"))),
            ("LinkedIn", bool(lead.social_links.get("linkedin"))),
        ) if ok
    ]
    if found:
        parts.append("Contact: " + ", ".join(found) + ".")
    return " ".join(parts)


# --- entry point --------------------------------------------------------------------


def score(lead: Lead, scoring: dict, market: Market, niche: Niche | None, year: int) -> Lead:
    """Score one lead — returns a new Lead, never mutates the input.

    A gated lead gets status "disqualified", its disqualify_reason, niche_match and
    lead_type, and no sub-scores. Every other lead gets need / viability / reach /
    priority / tier, score_reasons and score_confidence. The v3 fields
    (reachability_score / opportunity_score / total_score) stay None from v4 on.
    """
    case = audit_case(lead)
    lead_type, confidence = _CASES[case]
    match = niche_match(lead, niche)
    channel = best_channel(lead, market)
    common = dict(
        lead_type=lead_type,
        niche_match=match,
        audit_notes=_build_audit_notes(lead, case),
        reachability_score=None,
        opportunity_score=None,
        total_score=None,
        scoring_version=scoring["version"],
    )

    reason = gate(lead, scoring, market, match, channel)
    if reason:
        return replace(
            lead, **common, qualified=False, disqualify_reason=reason,
            need_score=None, viability_score=None, reach_score=None, priority=None,
            tier=None, score_reasons=[f"disqualified: {reason}"], score_confidence=None,
            status="disqualified",
        )

    need_value, need_reasons = need(lead, case, scoring, niche, year)
    via_value, via_reasons = viability(lead, scoring, market)
    reach_value = reach(channel, scoring, market)
    w = scoring["weights"]
    priority = round((w["need"] * need_value + w["viability"] * via_value) * (0.5 + reach_value / 200), 1)
    tier = tier_for(priority, scoring["tiers"])

    reasons: list[str] = []
    if match == "match":
        reasons.append("niche match")
    elif match == "mismatch":
        reasons.append(f"niche mismatch: {lead.maps_category}")
        tier = _cap(tier, "C")
    reasons += need_reasons + via_reasons + [f"best channel: {channel}"]
    if confidence == "low":
        tier = _cap(tier, "B")
        reasons.append("needs manual look")

    return replace(
        lead, **common, qualified=True, disqualify_reason=None,
        need_score=round(need_value, 1), viability_score=round(via_value, 1),
        reach_score=reach_value, priority=priority, tier=tier,
        score_reasons=reasons, score_confidence=confidence, status="scored",
    )
