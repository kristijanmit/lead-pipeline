"""score() is a pure function — hand-built Leads, hand-computed expectations,
no mocks (docs/ARCHITECTURE.md §13). A scoring bug silently misprioritizes every
lead, so the arithmetic here is written out longhand on purpose."""

import copy

import pytest

from pipeline.config import DEFAULTS
from pipeline.schema import Lead
from pipeline.scoring import score

# hand-built mirror of the config.yaml defaults — tests must not load config
WEIGHTS = {
    "version": 3,
    "reachability": {"has_email": 50, "has_phone": 40, "has_social": 10},
    "opportunity": {
        "no_website_score": 90,
        "audited_score_cap": 85,
        "no_https_bonus": 10,
        "not_mobile_friendly_bonus": 10,
        "lighthouse_weights": {
            "performance": 0.40,
            "best_practices": 0.25,
            "accessibility": 0.20,
            "seo": 0.15,
        },
    },
}

QUALIFICATION = {
    "target_countries": ["Serbia"],
}


def _lead(**overrides) -> Lead:
    defaults = dict(
        company="Test Dental",
        domain="test-dental.rs",
        source="osm",
        industry="dentist",
        country="Serbia",
    )
    return Lead(**{**defaults, **overrides})


def _score(lead, weights=WEIGHTS, qualification=QUALIFICATION):
    return score(lead, weights, qualification)


# --- qualification filters --------------------------------------------------


def test_country_not_in_target_fails_qualification():
    lead = _lead(country="Croatia", contact_emails=["a@b.rs"])
    scored = _score(lead)
    assert scored.qualified is False
    assert "Croatia" in scored.audit_notes


def test_any_industry_qualifies_and_is_left_as_collected():
    lead = _lead(industry="Literally anything, spaces & punctuation!!", contact_emails=["a@b.rs"])
    scored = _score(lead)
    assert scored.qualified is True
    assert scored.industry == lead.industry
    assert "target list" not in scored.audit_notes


def test_zero_reachability_not_qualified_even_though_filters_pass():
    lead = _lead(domain="")  # no contact info at all; country passes
    scored = _score(lead)
    assert scored.qualified is False
    assert scored.reachability_score == 0
    assert scored.opportunity_score is not None  # still computed (no_website case)


# --- reachability score / multiplier ---------------------------------------


@pytest.mark.parametrize(
    "emails,phones,socials,expected,multiplier",
    [
        ([], [], {}, 0, 0.2),
        ([], [], {"instagram": "x"}, 10, 0.5),
        ([], ["+381 21 111"], {}, 40, 0.7),
        ([], ["+381 21 111"], {"instagram": "x"}, 50, 0.7),
        (["a@b.rs"], [], {}, 50, 0.7),
        (["a@b.rs"], [], {"instagram": "x"}, 60, 0.7),
        (["a@b.rs"], ["+381 21 111"], {}, 90, 0.95),
        (["a@b.rs"], ["+381 21 111"], {"instagram": "x"}, 100, 1.0),
    ],
)
def test_reachability_score_and_multiplier_for_every_channel_combination(
    emails, phones, socials, expected, multiplier
):
    lead = _lead(domain="", contact_emails=emails, contact_phones=phones, social_links=socials)
    scored = _score(lead)
    assert scored.reachability_score == expected
    # domain="" -> opportunity is always the fixed no_website_score (90)
    assert scored.total_score == round(90 * multiplier, 1)


def test_reachability_ignores_website_quality_entirely():
    contacts = dict(contact_emails=["a@b.rs"], contact_phones=["+381 21 1"])
    awful_site = _lead(**contacts, lighthouse={"performance": 5}, https=False)
    no_site = _lead(**contacts, domain="")
    assert _score(awful_site).reachability_score == _score(no_site).reachability_score


# --- opportunity score -------------------------------------------------------


def test_no_website_lead_gets_fixed_score_and_new_build_type():
    scored = _score(_lead(domain="", contact_phones=["+381 21 111 222"]))
    assert scored.opportunity_score == 90
    assert scored.lead_type == "new_build"


def test_unclear_lead_type_leaves_opportunity_and_total_blank():
    lead = _lead(status="enrich_failed", errors=["audit: connection refused"])
    scored = _score(lead)
    assert scored.opportunity_score is None
    assert scored.total_score is None
    assert scored.lead_type == "unclear"
    assert "could not be audited" in scored.audit_notes


def test_unclear_lead_type_can_still_be_qualified_with_blank_opportunity():
    lead = _lead(contact_emails=["a@b.rs"])  # domain set, no lighthouse
    scored = _score(lead)
    assert scored.qualified is True
    assert scored.opportunity_score is None
    assert scored.total_score is None
    assert scored.reachability_score == 50


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
    scored = _score(lead)
    # quality = 40*0.40 + 60*0.25 + 80*0.20 + 90*0.15 = 16 + 15 + 16 + 13.5 = 60.5
    # opportunity = 100 - 60.5 = 39.5, + 10 (no https) + 10 (not mobile) = 59.5
    assert scored.opportunity_score == 59.5
    assert scored.lead_type == "redesign"


def test_https_and_mobile_bonuses_only_apply_when_explicitly_false():
    lighthouse = {"performance": 50, "accessibility": 50, "best_practices": 50, "seo": 50}
    healthy = _score(_lead(lighthouse=lighthouse, https=True, mobile_friendly=True))
    unknown = _score(_lead(lighthouse=lighthouse, https=None, mobile_friendly=None))
    assert healthy.opportunity_score == 50
    assert unknown.opportunity_score == 50  # unknown is not evidence of pain


def test_missing_lighthouse_category_renormalizes_instead_of_faking_opportunity():
    # only performance came back: quality is 50, not 50*0.40 = 20
    scored = _score(_lead(lighthouse={"performance": 50}))
    assert scored.opportunity_score == 50


def test_lighthouse_with_no_weighted_categories_falls_back_to_unreachable():
    # nothing to compute from — must not divide by zero, and "unclear" is
    # the honest lead_type when the audit produced no usable data
    scored = _score(_lead(lighthouse={"pwa": 30}))
    assert scored.opportunity_score is None
    assert scored.lead_type == "unclear"


def test_terrible_audited_site_is_capped_below_no_website_score():
    # raw would be 100 - 0 + 10 + 10 = 120 — must be capped, and the cap
    # must stay below no_website_score so a bad audited site never outranks
    # a lead with no website at all (docs/SCORING.md §4.2)
    lead = _lead(
        lighthouse={"performance": 0, "accessibility": 0, "best_practices": 0, "seo": 0},
        https=False,
        mobile_friendly=False,
    )
    scored = _score(lead)
    assert scored.opportunity_score == WEIGHTS["opportunity"]["audited_score_cap"]
    assert scored.opportunity_score < WEIGHTS["opportunity"]["no_website_score"]


def test_audited_score_cap_must_stay_below_no_website_score():
    bad_weights = {
        **WEIGHTS,
        "opportunity": {**WEIGHTS["opportunity"], "audited_score_cap": 95},
    }
    lead = _lead(lighthouse={"performance": 0, "accessibility": 0, "best_practices": 0, "seo": 0})
    with pytest.raises(AssertionError):
        _score(lead, weights=bad_weights)


# --- total score / audit notes ----------------------------------------------


def test_audit_notes_new_build_phone_only():
    lead = _lead(domain="", contact_phones=["+381 21 111 222"])
    assert _score(lead).audit_notes == (
        "Qualified: dentist, Serbia. No website: pitch a new build. "
        "Can be reached by phone. No email or social media found. "
        "Priority score 63/100: opportunity 90 (no website) x 0.7 for how "
        "reachable the contact details are (40/100)."
    )


def test_audit_notes_say_when_social_media_is_missing():
    lead = _lead(domain="", contact_emails=["a@b.rs"], contact_phones=["+381 21 111 222"])
    notes = _score(lead).audit_notes
    assert "Can be reached by email and phone. No social media found." in notes
    assert "Priority score 85.5/100: opportunity 90 (no website) x 0.95" in notes


def test_audit_notes_audited_site_lists_findings():
    lead = _lead(
        contact_emails=["a@b.rs"],
        lighthouse={"performance": 42, "accessibility": 88, "best_practices": 75, "seo": 91},
        https=False,
        mobile_friendly=False,
        cms="WordPress",
    )
    notes = _score(lead).audit_notes
    assert "Has a website (test-dental.rs): pitch a redesign." in notes
    assert "performance 42/100" in notes and "SEO 91/100" in notes
    assert "no HTTPS" in notes and "not mobile friendly" in notes and "built on WordPress" in notes


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
    scored = _score(lead)
    # reachability 100 -> multiplier 1.0; opportunity 59.5 -> total 59.5
    assert scored.reachability_score == 100
    assert scored.total_score == 59.5


def test_scoring_version_stamped_from_weights_and_nothing_else_changes():
    lead = _lead(domain="", contact_emails=["a@b.rs"])
    v1 = _score(lead, weights={**WEIGHTS, "version": 1})
    v3 = _score(lead, weights={**WEIGHTS, "version": 3})
    assert v1.scoring_version == 1
    assert v3.scoring_version == 3
    assert v1 == type(v1)(**{**v3.to_dict(), "scoring_version": 1})


def test_lead_type_and_opportunity_score_always_agree():
    # both derive from the single _audit_case branch — a new_build lead can
    # never carry a Lighthouse-computed score, and vice versa (docs/SCORING.md §9)
    leads = [
        _lead(domain=""),
        _lead(),  # domain, no lighthouse -> unclear
        _lead(lighthouse={"performance": 30, "seo": 80}),
    ]
    for lead in leads:
        scored = _score(lead)
        if scored.lead_type == "new_build":
            assert scored.opportunity_score == WEIGHTS["opportunity"]["no_website_score"]
        elif scored.lead_type == "unclear":
            assert scored.opportunity_score is None
        else:
            assert scored.lead_type == "redesign"
            assert scored.lighthouse is not None
            assert scored.opportunity_score is not None


def test_score_is_pure_and_marks_status_scored():
    lead = _lead(contact_emails=["a@b.rs"], status="enriched")
    before = copy.deepcopy(lead)
    scored = _score(lead)
    assert lead == before  # input never mutated
    assert scored is not lead
    assert scored.status == "scored"


def test_config_defaults_are_a_valid_weights_dict():
    scored = score(_lead(domain=""), DEFAULTS["scoring"], DEFAULTS["qualification"])
    assert scored.opportunity_score == 90
    assert scored.scoring_version == DEFAULTS["scoring"]["version"]


def test_audit_notes_include_rating_and_review_count():
    lead = _lead(domain="", contact_emails=["a@b.rs"], rating=4.9, review_count=127)
    assert "Google Maps: 4.9 stars from 127 reviews." in _score(lead).audit_notes
    assert "Google Maps" not in _score(_lead(domain="", contact_emails=["a@b.rs"])).audit_notes
