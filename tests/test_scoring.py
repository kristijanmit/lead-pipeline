"""score() v4 is a pure function — hand-built Leads, hand-computed expectations,
no mocks (docs/ARCHITECTURE.md §13). A scoring bug silently misprioritizes every
lead, so the arithmetic is written out longhand on purpose: one golden lead per
case (docs/plan/v2/script_edits.md §5), each asserting tier and reasons."""

import copy
import math

import pytest

from pipeline.config import DEFAULTS, ConfigError, load_config
from pipeline.market import Market
from pipeline.niche import Niche
from pipeline.schema import Lead
from pipeline.scoring import score

YEAR = 2026

# hand-built mirror of the config.yaml v4 block — tests must not load config
SCORING = {
    "version": 4,
    "gates": {
        "chain_listing_min": 3,
        "exclude_domains": ["bigchain.rs"],
        "exclude_categories": ["Pharmacy"],
        "niche_mismatch": False,
    },
    "weights": {"need": 0.6, "viability": 0.4},
    "reach_by_rank": [100, 90, 80, 60, 40],
    "need": {
        "new_build": {"base": 60, "has_social": 25, "reviews_20plus": 15},
        "dead_site_base": 50,
        "redesign": {
            "not_mobile": 30, "no_https": 20, "outdated": 20,
            "perf_below_50": 15, "no_booking_or_call": 10, "seo_below_70": 5,
        },
        "outdated_years": 4,
        "legacy_cms": ["Joomla"],
    },
    "viability": {
        "no_data": 50,
        "volume_at_strong": 40,
        "volume_max": 60,
        "rating_bands": [[4.5, 40], [4.0, 30], [3.5, 15]],
        "runs_ads": 10,
    },
    "tiers": {"A": 65, "B": 45, "C": 25},
}

SERBIA = Market(
    id="serbia", country="Serbia", dial_code="+381", mobile_prefixes=["+3816"], language="sr",
    channels=["Instagram", "Facebook", "Viber/Whatsapp", "Email", "Call"], review_strong=50,
    exclude_name_patterns=["dom zdravlja", "KBC"],
)
UAE = Market(
    id="uae", country="United Arab Emirates", dial_code="+971", mobile_prefixes=["+9715"], language="en",
    channels=["Instagram", "Facebook", "LinkedIn", "Email", "Call", "WhatsApp"],
    first_touch_excluded=["WhatsApp"], review_strong=100,
)
DENTAL = Niche(
    name="dental clinic", search_terms=["dentist"], phrase="dental clinics",
    accepted_categories=["Dentist", "Dental clinic"],
)

IG = {"instagram": "https://instagram.com/x"}
FB = {"facebook": "https://facebook.com/x"}
MOBILE_RS = "+381 64 555 2819"


def _lead(**overrides) -> Lead:
    defaults = dict(
        company="Test Dental", domain="test-dental.rs", source="maps",
        industry="dental clinic", country="Serbia", status="enriched",
    )
    return Lead(**{**defaults, **overrides})


def _score(lead, scoring=SCORING, market=SERBIA, niche=None):
    return score(lead, scoring, market, niche, YEAR)


def _volume(reviews, strong):
    return min(60, 40 * math.log10(reviews + 1) / math.log10(strong + 1))


def _priority(need, viability, reach):
    return (0.6 * need + 0.4 * viability) * (0.5 + reach / 200)


# --- golden leads, one per Need case ------------------------------------------------


def test_no_website_with_instagram_and_reviews():
    lead = _lead(domain="", social_links=IG, rating=4.7, review_count=212)
    s = _score(lead)
    viability = _volume(212, 50) + 40  # 54.5 + 40
    assert s.lead_type == "new_build"
    assert s.need_score == 100  # 60 + 25 social + 15 for 20+ reviews
    assert s.viability_score == pytest.approx(viability, abs=0.05)
    assert s.reach_score == 100
    assert s.priority == pytest.approx(_priority(100, viability, 100), abs=0.05)
    assert s.tier == "A"
    assert s.score_confidence == "high"
    assert s.score_reasons == [
        "no website", "already on Instagram/Facebook", "20+ reviews",
        "4.7★/212 reviews", "best channel: Instagram",
    ]
    assert s.status == "scored" and s.qualified is True


def test_social_page_as_domain_counts_as_no_website():
    s = _score(_lead(domain="facebook.com", social_links=FB))
    assert s.lead_type == "new_build"
    assert s.score_reasons[0] == "no website"


def test_dead_site_dns():
    lead = _lead(site_reachable=False, probe_error="dns", contact_emails=["info@test-dental.rs"])
    s = _score(lead)
    # need 50 · viability 50 (no data) · Email is rank 4 in Serbia -> reach 60 -> x0.8
    assert (s.need_score, s.viability_score, s.reach_score) == (50, 50, 60)
    assert s.priority == 40.0
    assert s.tier == "C"
    assert s.lead_type == "new_build"
    assert s.score_confidence == "high"
    assert "domain dead (dns)" in s.score_reasons
    assert "no rating data (viability 50)" in s.score_reasons


def test_certificate_error_uses_dead_site_need_with_high_confidence():
    lead = _lead(site_reachable=False, probe_error="ssl", social_links=FB)
    s = _score(lead)
    assert s.need_score == 75  # 50 + 25 social
    assert s.reach_score == 90  # Facebook
    assert s.priority == pytest.approx(_priority(75, 50, 90), abs=0.05)  # 61.75
    assert s.tier == "B"
    assert s.lead_type == "redesign"  # the site exists; only the Need formula is the dead-site one
    assert s.score_confidence == "high"
    assert s.score_reasons[0] == "certificate error, browsers show a warning"
    assert "needs manual look" not in s.score_reasons


@pytest.mark.parametrize("kind", ["timeout", "http_error", "other"])
def test_site_that_did_not_load_is_unclear_and_capped_at_b(kind):
    lead = _lead(site_reachable=False, probe_error=kind, social_links=IG, rating=4.8, review_count=25)
    s = _score(lead)
    viability = _volume(25, 50) + 40
    assert s.need_score == 90  # 50 + 25 + 15
    assert s.priority == pytest.approx(_priority(90, viability, 100), abs=0.05)  # 83.3 -> A
    assert s.tier == "B"  # low-confidence Need caps at B
    assert s.lead_type == "unclear"
    assert s.score_confidence == "low"
    assert s.score_reasons[0] == f"site didn't load ({kind})"
    assert s.score_reasons[-1] == "needs manual look"


def test_audited_good_site_has_near_zero_need():
    lead = _lead(
        site_reachable=True, https=True, mobile_friendly=True, has_viewport_meta=True,
        lighthouse={"performance": 90, "seo": 95}, copyright_year=2025,
        has_booking=True, has_click_to_call=True, social_links=IG, rating=4.5, review_count=60,
    )
    s = _score(lead)
    viability = _volume(60, 50) + 40
    assert s.need_score == 0
    assert s.priority == pytest.approx(_priority(0, viability, 100), abs=0.05)  # 32.7
    assert s.tier == "C"
    assert s.lead_type == "redesign"
    assert "site looks fine" in s.score_reasons


def test_audited_bad_site_gets_every_need_item():
    lead = _lead(
        site_reachable=True, https=False, mobile_friendly=False, has_viewport_meta=False,
        lighthouse={"performance": 30, "seo": 60}, copyright_year=2019,
        has_booking=False, has_click_to_call=False, has_messaging_link=False,
        contact_emails=["a@test-dental.rs"], contact_phones=[MOBILE_RS], rating=4.2, review_count=30,
    )
    s = _score(lead)
    viability = _volume(30, 50) + 30
    assert s.need_score == 100  # 30 + 20 + 20 + 15 + 10 + 5
    assert s.reach_score == 80  # Viber/Whatsapp, rank 3
    assert s.priority == pytest.approx(_priority(100, viability, 80), abs=0.05)  # 77.4
    assert s.tier == "A"
    assert s.score_reasons[:6] == [
        "not mobile-friendly", "no HTTPS", "outdated (© 2019)",
        "no booking, tap-to-call or messaging link", "slow site", "weak SEO basics",
    ]
    assert s.score_reasons[-1] == "best channel: Viber/Whatsapp"


def test_live_site_without_lighthouse_is_unclear_without_lighthouse_items():
    lead = _lead(
        site_reachable=True, https=True, has_viewport_meta=False, copyright_year=2018,
        has_booking=False, has_click_to_call=True, social_links=IG,
    )
    s = _score(lead)
    assert s.need_score == 50  # viewport stands in for mobile (30) + outdated (20)
    assert s.priority == 50.0  # (30 + 20) x 1.0
    assert s.tier == "B"
    assert s.lead_type == "unclear"
    assert s.score_confidence == "low"
    assert "needs manual look" in s.score_reasons


def test_old_run_without_signals_says_to_rescan():
    s = _score(_lead(https=False, social_links=IG))  # site_reachable None: pre-v11 data
    assert s.lead_type == "unclear"
    assert s.need_score == 20
    assert any("enrich --only audit --rescan" in r for r in s.score_reasons)


def test_osm_lead_without_rating_is_not_capped():
    s = _score(_lead(source="osm", domain="", social_links=IG))
    assert s.need_score == 85
    assert s.viability_score == 50
    assert s.priority == 71.0  # (51 + 20) x 1.0
    assert s.tier == "A"
    assert s.score_confidence == "high"
    assert "no rating data (viability 50)" in s.score_reasons


def test_messaging_link_counts_as_tap_to_call():
    base = dict(site_reachable=True, https=True, has_viewport_meta=True, has_booking=False,
                has_click_to_call=False, lighthouse={"performance": 80}, social_links=IG)
    assert _score(_lead(**base, has_messaging_link=True)).need_score == 0
    assert _score(_lead(**base, has_messaging_link=False)).need_score == 10


def test_legacy_builder_counts_as_outdated():
    lead = _lead(site_reachable=True, https=True, has_viewport_meta=True, cms="Joomla",
                 lighthouse={"performance": 80}, social_links=IG)
    s = _score(lead)
    assert s.need_score == 20
    assert "outdated builder (Joomla)" in s.score_reasons


def test_recent_copyright_is_not_outdated():
    lead = _lead(site_reachable=True, https=True, has_viewport_meta=True, copyright_year=2023,
                 lighthouse={"performance": 80}, social_links=IG)
    assert _score(lead).need_score == 0  # 2023 > 2026 - 4


def test_need_overrides_from_the_niche():
    niche = Niche(name="apartment", search_terms=["apartment"], phrase="apartmani",
                  need_overrides={"no_booking_or_call": 0})
    lead = _lead(site_reachable=True, https=True, has_viewport_meta=True, has_booking=False,
                 has_click_to_call=False, lighthouse={"performance": 80}, social_links=IG)
    s = _score(lead, niche=niche)
    assert s.need_score == 0
    assert "no booking, tap-to-call or messaging link" not in s.score_reasons


def test_need_is_capped_at_100():
    scoring = copy.deepcopy(SCORING)
    scoring["need"]["new_build"]["base"] = 90
    s = _score(_lead(domain="", social_links=IG, review_count=30), scoring=scoring)
    assert s.need_score == 100


# --- viability -------------------------------------------------------------------


def test_runs_ads_adds_viability():
    s = _score(_lead(domain="", social_links=IG, ads_tags=["meta_pixel"]))
    assert s.viability_score == 60  # 50 no data + 10
    assert "runs ads" in s.score_reasons


@pytest.mark.parametrize(
    "rating,points", [(4.9, 40), (4.5, 40), (4.4, 30), (4.0, 30), (3.9, 15), (3.5, 15), (3.4, 0)]
)
def test_rating_bands(rating, points):
    s = _score(_lead(domain="", social_links=IG, rating=rating, review_count=0))
    assert s.viability_score == points  # 0 reviews -> volume 0


def test_review_volume_is_capped():
    s = _score(_lead(domain="", social_links=IG, review_count=100000))
    assert s.viability_score == 60


def test_same_lead_in_serbia_and_uae_gets_market_bands():
    base = dict(domain="", rating=4.6, review_count=50, social_links={"linkedin": "https://linkedin.com/x"},
                contact_emails=["a@x.com"])
    rs = _score(_lead(**base), market=SERBIA)
    ae = _score(_lead(**base, country="United Arab Emirates"), market=UAE)
    assert rs.viability_score == 80  # 50 reviews = Serbia's review_strong -> volume 40, + 40
    assert ae.viability_score == pytest.approx(_volume(50, 100) + 40, abs=0.05)  # 74.1
    assert rs.reach_score == 60  # no LinkedIn channel in Serbia -> Email
    assert ae.reach_score == 80  # LinkedIn is rank 3 in the UAE


# --- L0 gates ---------------------------------------------------------------------


def _assert_disqualified(s, reason):
    assert s.status == "disqualified"
    assert s.qualified is False
    assert s.disqualify_reason == reason
    assert s.priority is None and s.tier is None and s.need_score is None
    assert s.score_reasons == [f"disqualified: {reason}"]


def test_country_gate():
    _assert_disqualified(_score(_lead(country="Croatia", social_links=IG)), "country: Croatia is not Serbia")


@pytest.mark.parametrize(
    "status,reason",
    [("Temporarily closed", "closed: temporarily"), ("Permanently closed", "closed: permanently"),
     ("CLOSED", "closed: permanently")],
)
def test_closed_gate(status, reason):
    _assert_disqualified(_score(_lead(business_status=status, social_links=IG)), reason)


def test_open_status_passes():
    assert _score(_lead(domain="", business_status="", social_links=IG)).status == "scored"


def test_chain_gate_by_listing_count():
    _assert_disqualified(_score(_lead(listing_count=3, social_links=IG)), "chain: 3 listings")
    assert _score(_lead(domain="", listing_count=2, social_links=IG)).status == "scored"


def test_chain_gate_by_excluded_domain():
    s = _score(_lead(domain="bigchain.rs", social_links=IG))
    _assert_disqualified(s, "chain: excluded domain bigchain.rs")


def test_excluded_category_gate():
    _assert_disqualified(_score(_lead(maps_category="pharmacy", social_links=IG)), "excluded category: pharmacy")


def test_market_name_pattern_gate_matches_whole_words():
    s = _score(_lead(company="Dom Zdravlja Novi Sad", social_links=IG))
    _assert_disqualified(s, "excluded name: dom zdravlja")
    _assert_disqualified(_score(_lead(company="KBC Zvezdara", social_links=IG)), "excluded name: KBC")
    assert _score(_lead(domain="", company="Kbcx Dental", social_links=IG)).status == "scored"


def test_name_patterns_are_per_market():
    lead = _lead(company="Dom Zdravlja Dubai", country="United Arab Emirates", domain="", social_links=IG)
    assert _score(lead, market=UAE).status == "scored"


def test_placeholder_phone_only_has_no_channel():
    s = _score(_lead(domain="", contact_phones=["+381 11 0000000"]))
    _assert_disqualified(s, "no usable contact channel")


def test_uae_whatsapp_is_never_a_first_touch():
    lead = _lead(domain="", country="United Arab Emirates", contact_phones=["+971 50 555 2819"])
    s = _score(lead, market=UAE)
    # the mobile is WhatsApp (excluded) but also a plain number: Call, rank 5 -> 40
    assert s.reach_score == 40
    assert s.score_reasons[-1] == "best channel: Call"


def test_gates_run_in_order():
    lead = _lead(country="Croatia", business_status="Permanently closed", listing_count=5)
    assert _score(lead).disqualify_reason.startswith("country")


# --- niche match ------------------------------------------------------------------


def test_niche_match_and_unknown():
    assert _score(_lead(domain="", social_links=IG, maps_category="dentist"), niche=DENTAL).niche_match == "match"
    assert "niche match" in _score(_lead(domain="", social_links=IG, maps_category="Dentist"), niche=DENTAL).score_reasons
    assert _score(_lead(domain="", social_links=IG), niche=DENTAL).niche_match == "unknown"  # no Maps category
    assert _score(_lead(domain="", social_links=IG, maps_category="Dentist")).niche_match == "unknown"  # no niche
    no_accepted = Niche(name="x", search_terms=["x"], phrase="x")
    assert _score(_lead(domain="", social_links=IG, maps_category="Dentist"), niche=no_accepted).niche_match == "unknown"


def test_niche_mismatch_caps_tier_at_c():
    lead = _lead(domain="", social_links=IG, rating=4.7, review_count=212, maps_category="Dental laboratory")
    s = _score(lead, niche=DENTAL)
    assert s.priority > 65
    assert s.tier == "C"
    assert s.niche_match == "mismatch"
    assert s.score_reasons[0] == "niche mismatch: Dental laboratory"


def test_niche_mismatch_gate_when_switched_on():
    scoring = copy.deepcopy(SCORING)
    scoring["gates"]["niche_mismatch"] = True
    s = _score(_lead(domain="", social_links=IG, maps_category="Pharmacy store"), scoring=scoring, niche=DENTAL)
    _assert_disqualified(s, "niche mismatch: Pharmacy store")
    assert s.niche_match == "mismatch"


# --- D2 and invariants --------------------------------------------------------------


def test_d2_bad_redesign_outranks_weak_new_build():
    bad_site = _score(_lead(
        site_reachable=True, https=False, mobile_friendly=False, lighthouse={"performance": 30, "seo": 60},
        copyright_year=2019, has_booking=False, has_click_to_call=False,
        contact_phones=[MOBILE_RS], rating=4.2, review_count=30,
    ))
    weak_new_build = _score(_lead(domain="", contact_emails=["a@x.rs"]))
    assert weak_new_build.priority == 44.8  # (36 + 20) x 0.8
    assert bad_site.priority > weak_new_build.priority


def test_audit_notes_hold_facts_only():
    lead = _lead(
        site_reachable=True, https=False, mobile_friendly=False, lighthouse={"performance": 30, "seo": 60},
        copyright_year=2019, cms="WordPress", rating=4.2, review_count=30, social_links=IG,
        location="Novi Sad",
    )
    notes = _score(lead).audit_notes
    assert notes.startswith("dental clinic, Novi Sad, Serbia.")
    assert "no HTTPS" in notes and "not mobile friendly" in notes and "copyright year 2019" in notes
    for banned in ("4.2", "30 reviews", "★", "stars", "Lighthouse", "Priority", "score"):
        assert banned not in notes


def test_score_is_pure_and_clears_v3_fields():
    lead = _lead(domain="", social_links=IG, total_score=80.0, opportunity_score=90.0, reachability_score=10.0)
    before = copy.deepcopy(lead)
    s = _score(lead)
    assert lead == before
    assert (s.total_score, s.opportunity_score, s.reachability_score) == (None, None, None)
    assert s.scoring_version == 4


def test_rescoring_a_disqualified_lead_that_now_passes_clears_the_reason():
    first = _score(_lead(domain="", listing_count=3, social_links=IG))
    second = _score(_lead(**{**first.to_dict(), "listing_count": 1}))
    assert second.status == "scored" and second.disqualify_reason is None


# --- config -----------------------------------------------------------------------


def test_defaults_match_the_hand_built_mirror_except_test_gates():
    defaults = copy.deepcopy(DEFAULTS["scoring"])
    mirror = copy.deepcopy(SCORING)
    mirror["gates"].update(exclude_domains=[], exclude_categories=[])
    assert defaults == mirror


def test_repo_config_yaml_is_scoring_v4():
    assert load_config("config.yaml")["scoring"] == DEFAULTS["scoring"]


def _write(tmp_path, scoring_yaml):
    path = tmp_path / "config.yaml"
    path.write_text(scoring_yaml)
    return path


def test_v3_scoring_block_fails_with_a_pointer(tmp_path):
    path = _write(tmp_path, "scoring:\n  version: 3\n  reachability: {has_email: 50}\n")
    with pytest.raises(ConfigError, match="scoring v4 expected.*SCORING.md"):
        load_config(path)


@pytest.mark.parametrize(
    "yaml_text,message",
    [
        ("scoring:\n  weights: {need: 0.7, viability: 0.4}\n", "must sum to 1.0"),
        ("scoring:\n  tiers: {A: 40, B: 45, C: 25}\n", "must descend"),
        ("scoring:\n  reach_by_rank: [100, 120]\n", "reach_by_rank"),
        ("scoring:\n  gates: {niche_mismatch: yes please}\n", "niche_mismatch"),
        ("scoring:\n  need: {redesign: {has_booking: 5}}\n", "need.redesign must have exactly"),
        ("scoring:\n  viability: {rating_bands: [[4.0, 30], [4.5, 40]]}\n", "highest rating first"),
    ],
)
def test_invalid_scoring_values_fail(tmp_path, yaml_text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, yaml_text))


def test_niche_need_overrides_are_validated():
    with pytest.raises(ValueError, match="unknown item 'has_booking'"):
        Niche.from_dict({"name": "x", "search_terms": ["x"], "phrase": "x", "need_overrides": {"has_booking": 0}})
    with pytest.raises(ValueError, match="0-100"):
        Niche.from_dict({"name": "x", "search_terms": ["x"], "phrase": "x", "need_overrides": {"outdated": 500}})
    niche = Niche.from_dict({"name": "x", "search_terms": ["x"], "phrase": "x", "need_overrides": {"outdated": 0}})
    assert niche.need_overrides == {"outdated": 0}
