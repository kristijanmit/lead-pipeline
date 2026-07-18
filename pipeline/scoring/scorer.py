"""Pure scoring function — score(lead, weights, qualification) -> Lead, no I/O.

The "why" behind every number lives in docs/SCORING.md; the short version:
qualification (industry + country) is a hard filter, not a score — it's too
early to know which vertical converts best, so weighting industries now
would be guessing. Reachability (can we contact them) and Opportunity (how
much upside in a redesign/new build) are kept as separate sub-scores so a
high or low total is always explainable. opportunity_score runs in the
"pain" direction — higher means more to sell into, not a healthier site.

Keep this module free of I/O, network calls, and config loading
(docs/ARCHITECTURE.md §13) — weights/qualification arrive as plain dicts from
config.yaml.
"""

from __future__ import annotations

from dataclasses import replace

from pipeline.schema import Lead

# _audit_case result -> lead_type. new_build: no domain, pitch a site from
# scratch. redesign: audited, pitch improving what exists. unclear: domain
# exists but couldn't be audited — could be a live site temporarily down or
# an abandoned domain; needs a manual look before either pitch (docs/SCORING.md §8).
_LEAD_TYPES = {
    "no_website": "new_build",
    "unreachable": "unclear",
    "audited": "redesign",
}

# Total Score = opportunity_score * multiplier, keyed off reachability_score.
# A fixed policy table (docs/SCORING.md §4), not a tunable weight — reachability
# only ever lands on one of {0, 10, 40, 50, 60, 90, 100} given the point
# values above, but the bucket checks below cover the full 0-100 domain.
def _reachability_multiplier(reachability_score: float) -> float:
    if reachability_score == 100:
        return 1.0
    if reachability_score == 90:
        return 0.95
    if 40 <= reachability_score <= 89:
        return 0.7
    if 1 <= reachability_score <= 39:
        return 0.5
    return 0.2  # reachability_score == 0


def _audit_case(lead: Lead, lighthouse_weights: dict) -> str:
    """The single branch opportunity_score and lead_type both derive from —
    one helper so the two can never disagree about which case a lead is.

    A lighthouse dict sharing no categories with the configured weights has
    nothing to compute from, so it lands in "unreachable" too — degrade,
    don't crash on a divide-by-zero (and "unclear" is the honest lead_type
    when the audit produced no usable data).
    """
    if not lead.domain:
        return "no_website"
    if not lead.lighthouse or not any(m in lead.lighthouse for m in lighthouse_weights):
        return "unreachable"
    return "audited"


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _classify_industry(raw_industry: str, industry_map: dict) -> str | None:
    """Case-insensitive lookup of a raw collected industry value against the
    qualification.industry_map synonym table. Returns the canonical target
    category (e.g. "clinic") if raw_industry matches a known synonym for it,
    else None — an unrecognized value simply fails the filter, it never
    raises, since the raw industry string comes from free-form OSM tags /
    maps categories / manual CSV input and can be anything.
    """
    needle = (raw_industry or "").strip().lower()
    if not needle:
        return None
    for category, synonyms in industry_map.items():
        if needle == category.lower() or needle in {s.lower() for s in synonyms}:
            return category
    return None


def _qualifies(lead: Lead, qualification: dict) -> tuple[bool, bool, str | None]:
    """Returns (industry_ok, country_ok, canonical_industry). canonical_industry
    is the resolved target category to fold lead.industry into, or None if
    industry_ok is False.
    """
    canonical = _classify_industry(lead.industry, qualification["industry_map"])
    industry_ok = canonical is not None
    targets = {c.strip().lower() for c in qualification["target_countries"]}
    country_ok = (lead.country or "").strip().lower() in targets
    return industry_ok, country_ok, canonical


def _reachability_score(lead: Lead, weights: dict) -> float:
    """0-100: can we actually contact this lead right now, today."""
    score = 0.0
    if lead.contact_emails:
        score += weights["has_email"]
    if lead.contact_phones:
        score += weights["has_phone"]
    if lead.social_links:
        score += weights["has_social"]
    return _clamp(score)


def _opportunity_score(lead: Lead, case: str, weights: dict) -> float | None:
    """Opportunity, not quality — the inverse of a site-health score.

    New-build (no domain) leads get a fixed policy score. Unclear leads
    (domain present, no usable Lighthouse data) get no opportunity_score at
    all — there's nothing to compute from, and defaulting to either the
    new-build or a discounted fixed value would be guessing which pitch
    applies (docs/SCORING.md §3). Only "audited" leads run the weighted formula.
    """
    if case == "no_website":
        return float(weights["no_website_score"])
    if case == "unreachable":
        return None

    lighthouse_weights = weights["lighthouse_weights"]
    present = [m for m in lighthouse_weights if m in lead.lighthouse]
    weight_total = sum(lighthouse_weights[m] for m in present)
    # renormalize over the categories Lighthouse actually returned, so a
    # missing category reads as "no signal" rather than fake opportunity
    weighted_quality = (
        sum(lead.lighthouse[m] * lighthouse_weights[m] for m in present) / weight_total
    )
    opportunity = 100.0 - weighted_quality
    # flat bonuses: Lighthouse doesn't penalize these cleanly, and each is a
    # one-sentence hook in a pitch that a performance number isn't
    if lead.https is False:
        opportunity += weights["no_https_bonus"]
    if lead.mobile_friendly is False:
        opportunity += weights["not_mobile_friendly_bonus"]

    # A bad audited site is still an easier sell than no website at all —
    # there's already an owner convinced they need a site, no domain/DNS
    # setup, and a live baseline to point at. Capping here keeps even the
    # worst audited site below no_website_score so the ranking can never
    # invert that (docs/SCORING.md §4.2). Config validation already rejects a
    # cap >= no_website_score at startup; this assertion is a second guard
    # for weights dicts built by hand (e.g. in tests) that skip that check.
    cap = weights["audited_score_cap"]
    assert cap < weights["no_website_score"], (
        "audited_score_cap must stay below no_website_score, or a bad "
        "audited site could outrank a lead with no website at all"
    )
    return _clamp(min(opportunity, cap))


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _reachability_breakdown(lead: Lead) -> str:
    parts = []
    if lead.contact_emails:
        parts.append("email")
    if lead.contact_phones:
        parts.append("phone")
    if lead.social_links:
        parts.append("social")
    if not parts:
        return "none"
    if len(parts) == 1:
        return f"{parts[0]} only"
    return " + ".join(parts)


def _build_audit_notes(
    lead: Lead,
    *,
    industry_ok: bool,
    country_ok: bool,
    canonical_industry: str | None,
    reachability: float | None,
    multiplier: float | None,
    opportunity: float | None,
    total: float | None,
    case: str | None,
) -> str:
    if not (industry_ok and country_ok):
        reasons = []
        if not industry_ok:
            reasons.append(f"industry '{lead.industry}' not in target list")
        if not country_ok:
            reasons.append(f"country '{lead.country or 'unknown'}' not in scope")
        return f"Not qualified: {'; '.join(reasons)}."

    breakdown = _reachability_breakdown(lead)
    if case == "unreachable":
        opportunity_clause = "Opportunity: unclear lead type, needs classification."
    elif case == "no_website":
        opportunity_clause = f"Opportunity: new-build policy ({_fmt(opportunity)})."
    else:
        opportunity_clause = f"Opportunity: audit score ({_fmt(opportunity)})."

    reachability_clause = (
        f"Reachability: {breakdown} ({_fmt(reachability)}, x{multiplier} multiplier)."
    )
    total_clause = f"Total: {_fmt(total)}."

    prefix = "Qualified" if reachability and reachability > 0 else (
        "Not qualified (no contact info — needs further enrichment)"
    )
    return (
        f"{prefix}: {canonical_industry}, {lead.country}. "
        f"{opportunity_clause} {reachability_clause} {total_clause}"
    )


def score(lead: Lead, weights: dict, qualification: dict) -> Lead:
    """Score one lead — returns a new Lead, never mutates the input.

    Qualification (industry + country) runs first and gates everything else
    (docs/SCORING.md §1): a lead that fails either filter gets no
    reachability_score/opportunity_score/total_score and is never marked
    qualified. A lead that passes both but has zero reachability_score still
    gets scored (so it doesn't rank artificially high if the multiplier
    logic changes later) but is also not marked qualified — it needs more
    enrichment before outreach, not a ranking.
    """
    case = _audit_case(lead, weights["opportunity"]["lighthouse_weights"])
    lead_type = _LEAD_TYPES[case]
    industry_ok, country_ok, canonical_industry = _qualifies(lead, qualification)

    if not (industry_ok and country_ok):
        return replace(
            lead,
            reachability_score=None,
            opportunity_score=None,
            total_score=None,
            lead_type=lead_type,
            qualified=False,
            audit_notes=_build_audit_notes(
                lead,
                industry_ok=industry_ok,
                country_ok=country_ok,
                canonical_industry=canonical_industry,
                reachability=None,
                multiplier=None,
                opportunity=None,
                total=None,
                case=case,
            ),
            scoring_version=weights["version"],
            status="scored",
        )

    reachability = _reachability_score(lead, weights["reachability"])
    multiplier = _reachability_multiplier(reachability)
    opportunity = _opportunity_score(lead, case, weights["opportunity"])
    total = round(opportunity * multiplier, 1) if opportunity is not None else None

    return replace(
        lead,
        industry=canonical_industry,
        reachability_score=round(reachability, 1),
        opportunity_score=round(opportunity, 1) if opportunity is not None else None,
        total_score=total,
        lead_type=lead_type,
        qualified=reachability > 0,
        audit_notes=_build_audit_notes(
            lead,
            industry_ok=industry_ok,
            country_ok=country_ok,
            canonical_industry=canonical_industry,
            reachability=reachability,
            multiplier=multiplier,
            opportunity=opportunity,
            total=total,
            case=case,
        ),
        scoring_version=weights["version"],
        status="scored",
    )
