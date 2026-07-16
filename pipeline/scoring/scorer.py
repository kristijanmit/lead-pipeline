"""Pure scoring function — score(lead, weights) -> Lead, no I/O.

The "why" behind every weight lives in SCORING.md; the short version: fit
(is this a reachable, real business) and opportunity (how bad is their web
presence) are different questions, kept as separate sub-scores so a high or
low total is always explainable. website_audit_score runs in the "pain"
direction — higher means more to sell into, not a healthier site.

Keep this module free of I/O, network calls, and config loading
(ARCHITECTURE.md §13) — weights arrive as a plain dict from config.yaml.
"""

from __future__ import annotations

from dataclasses import replace

from pipeline.schema import Lead

# _audit_case result -> lead_type. new_build: no domain, pitch a site from
# scratch. redesign: audited, pitch improving what exists. unclear: domain
# exists but couldn't be audited — could be a live site temporarily down or
# an abandoned domain; needs a manual look before either pitch (SCORING.md §8).
_LEAD_TYPES = {
    "no_website": "new_build",
    "unreachable": "unclear",
    "audited": "redesign",
}


def _audit_case(lead: Lead, lighthouse_weights: dict) -> str:
    """The single branch website_audit_score and lead_type both derive from —
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


def _icp_fit_score(lead: Lead, weights: dict) -> float:
    """Reachability and legitimacy — deliberately blind to the website
    (no lighthouse/https/cms here; that's website_audit_score's job)."""
    score = 0.0
    if lead.contact_emails:
        score += weights["has_email"]
    if lead.contact_phones:
        score += weights["has_phone"]
    if lead.social_links:
        score += weights["has_social"]
    # distinct channel *types* present, not total item count — two phone
    # numbers and nothing else isn't "established" the same way one phone
    # plus one email is; this is about breadth of reachability, not depth
    channels_present = sum(
        [bool(lead.contact_emails), bool(lead.contact_phones), bool(lead.social_links)]
    )
    if channels_present >= weights["established_threshold"]:
        score += weights["established_bonus"]
    # currently a flat, always-awarded credit while collection is
    # single-industry per run, so it contributes no discriminating signal
    # today — schema-forward for when a run mixes industries, kept as a
    # named component now so that becomes a weight change, not a schema
    # change (SCORING.md §3)
    score += weights["industry_match"]
    return _clamp(score)


def _website_audit_score(lead: Lead, case: str, weights: dict) -> float:
    """Opportunity, not quality — the inverse of a site-health score.

    No-domain and unreachable-domain leads get fixed scores: there is no
    Lighthouse data to compute from, and falling through to the formula
    would zero out two of the strongest opportunity signals in the batch.
    """
    if case == "no_website":
        return float(weights["no_website_score"])
    if case == "unreachable":
        return float(weights["unreachable_domain_score"])

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
    # invert that (SCORING.md §4.2). Config validation already rejects a
    # cap >= no_website_score at startup; this assertion is a second guard
    # for weights dicts built by hand (e.g. in tests) that skip that check.
    cap = weights["audited_score_cap"]
    assert cap < weights["no_website_score"], (
        "audited_score_cap must stay below no_website_score, or a bad "
        "audited site could outrank a lead with no website at all"
    )
    return _clamp(min(opportunity, cap))


def research_rank(lead: Lead, weights: dict) -> float:
    """icp_fit and website_audit only, excluding intent — used to build the
    manual intent-research candidate pool (SCORING.md §5).

    Pre-research, intent_score is 0.0 for every lead, so total_score is just
    this same weighted sum plus a uniform zero. Exposing the sum on its own
    (rather than reusing total_score) makes that explicit at the call site
    instead of relying on the reader to know intent contributes nothing yet.
    """
    total_weights = weights["total"]
    return (
        lead.icp_fit_score * total_weights["icp_fit"]
        + lead.website_audit_score * total_weights["website_audit"]
    )


def score(lead: Lead, weights: dict) -> Lead:
    """Score one lead — returns a new Lead, never mutates the input.

    intent_score is passed through untouched: it is manual-only, entered by
    hand after researching the top candidates (SCORING.md §5). scorer.py
    never writes it.
    """
    case = _audit_case(lead, weights["website_audit"]["lighthouse_weights"])
    icp_fit = _icp_fit_score(lead, weights["icp_fit"])
    website_audit = _website_audit_score(lead, case, weights["website_audit"])
    total_weights = weights["total"]
    total = (
        icp_fit * total_weights["icp_fit"]
        + website_audit * total_weights["website_audit"]
        + lead.intent_score * total_weights["intent"]
    )
    return replace(
        lead,
        icp_fit_score=round(icp_fit, 1),
        website_audit_score=round(website_audit, 1),
        total_score=round(total, 1),
        lead_type=_LEAD_TYPES[case],
        scoring_version=weights["version"],
        status="scored",
    )
