"""score() is a pure function — hand-built Leads, hand-computed expectations,
no mocks (ARCHITECTURE.md §13). A scoring bug silently misprioritizes every
lead, so the arithmetic here is written out longhand on purpose."""

import copy

import pytest

from pipeline.config import DEFAULTS
from pipeline.schema import Lead
from pipeline.scoring import score

# hand-built mirror of the config.yaml defaults — tests must not load config
WEIGHTS = {
    "version": 1,
    "icp_fit": {
        "has_email": 30,
        "has_phone": 25,
        "has_social": 15,
        "established_bonus": 15,
        "established_threshold": 2,
        "industry_match": 15,
    },
    "website_audit": {
        "no_website_score": 90,
        "unreachable_domain_score": 65,
        "no_https_bonus": 10,
        "not_mobile_friendly_bonus": 10,
        "lighthouse_weights": {
            "performance": 0.40,
            "best_practices": 0.25,
            "accessibility": 0.20,
            "seo": 0.15,
        },
    },
    "total": {"icp_fit": 0.35, "website_audit": 0.50, "intent": 0.15},
}


def _lead(**overrides) -> Lead:
    defaults = dict(company="Test Dental", domain="test-dental.rs", source="osm")
    return Lead(**{**defaults, **overrides})


def test_no_website_lead_gets_fixed_score_and_new_build_type():
    scored = score(_lead(domain="", contact_phones=["+381 21 111 222"]), WEIGHTS)
    assert scored.website_audit_score == 90
    assert scored.lead_type == "new_build"


def test_unreachable_domain_gets_fixed_score_and_unclear_type():
    lead = _lead(status="enrich_failed", errors=["audit: connection refused"])
    scored = score(lead, WEIGHTS)
    assert scored.website_audit_score == 65
    assert scored.lead_type == "unclear"


def test_audited_lead_matches_hand_computed_opportunity():
    lead = _lead(
        lighthouse={
            "performance": 40,
            "accessibility": 80,
            "best_practices": 60,
            "seo": 90,
        },
        https=False,
        mobile_friendly=False,
    )
    scored = score(lead, WEIGHTS)
    # quality = 40*0.40 + 60*0.25 + 80*0.20 + 90*0.15 = 16 + 15 + 16 + 13.5 = 60.5
    # opportunity = 100 - 60.5 = 39.5, + 10 (no https) + 10 (not mobile) = 59.5
    assert scored.website_audit_score == 59.5
    assert scored.lead_type == "redesign"


def test_https_and_mobile_bonuses_only_apply_when_explicitly_false():
    lighthouse = {"performance": 50, "accessibility": 50, "best_practices": 50, "seo": 50}
    healthy = score(_lead(lighthouse=lighthouse, https=True, mobile_friendly=True), WEIGHTS)
    unknown = score(_lead(lighthouse=lighthouse, https=None, mobile_friendly=None), WEIGHTS)
    assert healthy.website_audit_score == 50
    assert unknown.website_audit_score == 50  # unknown is not evidence of pain


def test_missing_lighthouse_category_renormalizes_instead_of_faking_opportunity():
    # only performance came back: quality is 50, not 50*0.40 = 20
    scored = score(_lead(lighthouse={"performance": 50}), WEIGHTS)
    assert scored.website_audit_score == 50


def test_lighthouse_with_no_weighted_categories_falls_back_to_unreachable():
    # nothing to compute from — must not divide by zero, and "unclear" is
    # the honest lead_type when the audit produced no usable data
    scored = score(_lead(lighthouse={"pwa": 30}), WEIGHTS)
    assert scored.website_audit_score == 65
    assert scored.lead_type == "unclear"


def test_terrible_audited_site_clamps_at_100():
    lead = _lead(
        lighthouse={"performance": 0, "accessibility": 0, "best_practices": 0, "seo": 0},
        https=False,
        mobile_friendly=False,
    )
    assert score(lead, WEIGHTS).website_audit_score == 100


def test_icp_fit_full_contact_surface():
    lead = _lead(
        contact_emails=["office@test-dental.rs"],
        contact_phones=["+381 21 111 222"],
        social_links={"instagram": "https://instagram.com/testdental"},
    )
    # 30 (email) + 25 (phone) + 15 (social) + 15 (surface 3 >= 2) + 15 (industry) = 100
    assert score(lead, WEIGHTS).icp_fit_score == 100


def test_icp_fit_phone_only_practice():
    lead = _lead(contact_phones=["+381 21 111 222"])
    # 25 (phone) + 15 (industry); surface 1 < threshold 2, no established bonus
    assert score(lead, WEIGHTS).icp_fit_score == 40


def test_icp_fit_ignores_website_quality_entirely():
    contacts = dict(contact_emails=["a@b.rs"], contact_phones=["+381 21 1"])
    awful_site = _lead(**contacts, lighthouse={"performance": 5}, https=False)
    no_site = _lead(**contacts, domain="")
    assert score(awful_site, WEIGHTS).icp_fit_score == score(no_site, WEIGHTS).icp_fit_score


def test_intent_score_passes_through_untouched():
    # the one invariant that must never break (SCORING.md §5/§9)
    scored = score(_lead(domain="", intent_score=80.0), WEIGHTS)
    assert scored.intent_score == 80.0
    # icp_fit = 15 (industry only), audit = 90 (no website)
    # total = 15*0.35 + 90*0.50 + 80*0.15 = 5.25 + 45 + 12 = 62.25 -> 62.2 (banker's)
    assert scored.total_score == round(15 * 0.35 + 90 * 0.50 + 80 * 0.15, 1)


def test_total_score_hand_computed():
    lead = _lead(
        contact_emails=["office@test-dental.rs"],
        contact_phones=["+381 21 111 222"],
        social_links={"instagram": "https://instagram.com/testdental"},
        lighthouse={
            "performance": 40,
            "accessibility": 80,
            "best_practices": 60,
            "seo": 90,
        },
        https=False,
        mobile_friendly=False,
    )
    scored = score(lead, WEIGHTS)
    # icp 100, audit 59.5, intent 0: 100*0.35 + 59.5*0.50 + 0 = 35 + 29.75 = 64.75
    assert scored.total_score == round(100 * 0.35 + 59.5 * 0.50, 1)


def test_scoring_version_stamped_from_weights_and_nothing_else_changes():
    lead = _lead(domain="", contact_emails=["a@b.rs"])
    v1 = score(lead, WEIGHTS)
    v2 = score(lead, {**WEIGHTS, "version": 2})
    assert v1.scoring_version == 1
    assert v2.scoring_version == 2
    assert v1 == type(v1)(**{**v2.to_dict(), "scoring_version": 1})


def test_lead_type_and_audit_score_always_agree():
    # both derive from the single _audit_case branch — a new_build lead can
    # never carry a Lighthouse-computed score, and vice versa (SCORING.md §9)
    fixed = {
        "new_build": WEIGHTS["website_audit"]["no_website_score"],
        "unclear": WEIGHTS["website_audit"]["unreachable_domain_score"],
    }
    leads = [
        _lead(domain=""),
        _lead(),  # domain, no lighthouse
        _lead(lighthouse={"performance": 30, "seo": 80}),
    ]
    for lead in leads:
        scored = score(lead, WEIGHTS)
        if scored.lead_type in fixed:
            assert scored.website_audit_score == fixed[scored.lead_type]
        else:
            assert scored.lead_type == "redesign"
            assert scored.lighthouse is not None


def test_score_is_pure_and_marks_status_scored():
    lead = _lead(contact_emails=["a@b.rs"], status="enriched")
    before = copy.deepcopy(lead)
    scored = score(lead, WEIGHTS)
    assert lead == before  # input never mutated
    assert scored is not lead
    assert scored.status == "scored"


def test_config_defaults_are_a_valid_weights_dict():
    scored = score(_lead(domain=""), DEFAULTS["scoring"])
    assert scored.website_audit_score == 90
    assert scored.scoring_version == DEFAULTS["scoring"]["version"]


@pytest.mark.parametrize("case_domain", ["", "test-dental.rs"])
def test_every_scored_lead_has_all_score_fields_set(case_domain):
    scored = score(_lead(domain=case_domain), WEIGHTS)
    assert scored.icp_fit_score is not None
    assert scored.website_audit_score is not None
    assert scored.total_score is not None
    assert scored.lead_type in {"new_build", "redesign", "unclear"}
