"""Drafting rule sets per language (v2 step 7, script_edits.md §9.2, §10): the en/sr
validators, examples selection on the real examples files, the niche-neutral question
rule, first_offer, LinkedIn's two outputs. The claude CLI call is faked."""

from pathlib import Path

import pytest
import yaml

from pipeline.config import DEFAULTS, ConfigError, load_config
from pipeline.drafting import drafter
from pipeline.drafting import rules_en, rules_sr
from pipeline.drafting.rules import validate_connection_note, validate_message
from pipeline.market import Market
from pipeline.niche import Niche
from pipeline.schema import Lead
from support import MARKETS_YAML

ROOT = Path(__file__).resolve().parent.parent
EN_FILE = drafter.load_examples(str(ROOT / "docs/plan/v2/outreach.en.md"))
SR_FILE = drafter.load_examples(str(ROOT / "docs/plan/v2/outreach.sr.md"))
EN, SR = rules_en.RULES, rules_sr.RULES


def _market(market_id):
    return Market.from_dict(market_id, yaml.safe_load(MARKETS_YAML)["markets"][market_id])


def _lead(**kw):
    kw.setdefault("market", "uae")
    kw.setdefault("language", "en")
    kw.setdefault("social_links", {"instagram": "https://instagram.com/x"})
    return Lead(company=kw.pop("company", "Smile Dental"), domain=kw.pop("domain", ""), source="maps", **kw)


def _outreach(**kw):
    return {**DEFAULTS["outreach"], **kw}


# --- rules_en validator ----------------------------------------------------------

def test_en_accepts_a_clean_dm_email_and_call():
    assert validate_message("Hi, I came across your profile. How do new patients find you?", "Instagram", EN) == []
    email = (
        "Subject: Quick question\n\nHello, this is Kristijan. How do new clients find you?\n\n"
        "If this isn't relevant, just let me know.\n\nKristijan, ONIX Web Lab, onixweblab.com"
    )
    assert validate_message(email, "Email", EN) == []  # the signature domain is exempt
    assert validate_message("Hello, this is Kristijan. Do you have a minute?", "Call", EN) == []


@pytest.mark.parametrize("message, problem", [
    ("Hi, great work! How do clients find you?", "exclamation"),
    ("Hi, I can build a free site. Interested?", "'free'"),
    ("Hi, see onixweblab.com. Interested?", "link"),
    ("Hi, see https://x.io/a. Interested?", "link"),
    ("Hi, how do clients find you? Do you use Google?", "2 questions"),
    ("Hi, مرحبا. How do clients find you?", "Arabic"),
    ("Hi, Привет. How do clients find you?", "Cyrillic"),
    ("Hello, how do clients find you?", "must start with 'Hi'"),
    ("Hi, I'm Kristijan.", "one question"),
    ("Hi, your 4.9 rating is great. How do clients find you?", "'rating'"),
    ("Hi, you have many reviews. How do clients find you?", "'review'"),
    ("Hi, I build AI websites. Curious?", "'AI'"),
    ("Hi, I see you don't have a website. Curious?", "don't have a website"),
    ("Hi, I hope this finds you well. Curious?", "hope this finds you"),
    ("Hi {first_name}, how do clients find you?", "slot"),
])
def test_en_rejects(message, problem):
    assert any(problem in p for p in validate_message(message, "Instagram", EN)), validate_message(message, "Instagram", EN)


def test_en_whole_word_bans_do_not_hit_ordinary_words():
    ok = "Hi, I said I maintain sites for freelancers and painters. How do clients find you?"
    assert not any("banned" in p for p in validate_message(ok, "Instagram", EN))


def test_en_link_allowed_when_links_are_turned_on():
    msg = "Hi, here is onixweblab.com/clinics. How do patients find you?"
    assert validate_message(msg, "Instagram", EN, allow_link=True) == []


@pytest.mark.parametrize("term", ["patient records", "Patient-Records", "EHR", "emr", "medical records"])
def test_platform_terms_banned_in_english(term):
    msg = f"Hi, I saw your {term} setup. How do patients find you?"
    assert any("patient-records platform" in p for p in validate_message(msg, "Instagram", EN))


@pytest.mark.parametrize("term", ["karton pacijenta", "elektronski karton", "Medicinska dokumentacija",
                                  "evidencija pacijenata", "kartoni pacijenata"])
def test_platform_terms_banned_in_serbian_even_on_viber(term):
    msg = f"Pozdrav, vidim {term}. Kako Vam stižu upiti?"
    for channel in ("Instagram", "Viber/Whatsapp"):
        assert any("patient-records platform" in p for p in validate_message(msg, channel, SR))
    assert not any("platform" in p for p in validate_message("Pozdrav, Kehrer. Pitanje?", "Instagram", SR))


def test_connection_note_limit_and_no_question_needed():
    assert validate_connection_note("Hi, I came across your clinic in Dubai. I build websites for clinics.", EN) == []
    long = "Hi, " + "a" * 300
    assert any("limit is 300" in p for p in validate_connection_note(long, EN))
    assert any("must start with 'Hi'" in p for p in validate_connection_note("Hello there.", EN))
    assert any(p.startswith("connection note:") for p in validate_connection_note("Hi, free stuff.", EN))


def test_sr_rules_unchanged_by_the_split():
    assert validate_message("Pozdrav. Kako Vam stižu upiti?", "Instagram", SR) == []
    assert SR.max_questions is None and not SR.no_links  # the Viber format keeps two questions and a link line


# --- examples selection on the real files ------------------------------------------

def test_en_examples_match_niche_alias_and_fall_back_to_generic():
    clinic = drafter.select_examples(EN_FILE, "Instagram", "dental clinic")
    assert "before-and-after posts" in clinic and "apartments look really clean" not in clinic
    assert "Questions by business type" in clinic and "friendly warning" in clinic
    other = drafter.select_examples(EN_FILE, "Instagram", "yoga studio")
    assert "like the way you present your services" in other and "before-and-after" not in other
    assert "one-page note" not in clinic and "one-page note" not in other  # audit offer off by default
    assert "<!--" not in EN_FILE  # operator comments never reach the model


def test_audit_offer_sections_only_when_first_offer_is_audit():
    audit = drafter.select_examples(EN_FILE, "Instagram", "yoga studio", offer="audit")
    assert "one-page note with three concrete suggestions" in audit
    assert "how new clients usually find you?" not in audit
    fb = drafter.select_examples(EN_FILE, "Facebook", "yoga studio", offer="audit")
    assert "one-page note" not in fb  # no audit section for Facebook -> question
    sr = drafter.select_examples(SR_FILE, "Instagram", "frizer")
    assert "jednu stranu sa tri" not in sr
    assert "jednu stranu sa tri" in drafter.select_examples(SR_FILE, "Instagram", "frizer", offer="audit")


def test_linkedin_examples_keep_connection_note_and_matching_after_accept():
    clinic = drafter.select_examples(EN_FILE, "LinkedIn", "dental clinic")
    assert "would be glad to connect" in clinic and "offers {specific treatment}" in clinic
    assert "presents its work" not in clinic
    other = drafter.select_examples(EN_FILE, "LinkedIn", "law firm")
    assert "would be glad to connect" in other and "presents its work" in other
    assert "{specific treatment}" not in other


def test_sr_file_headings_route_clinic_and_generic():
    clinic = drafter.select_examples(SR_FILE, "Instagram", "stomatološka ordinacija")
    dm = clinic.split("## Instagram (DM) with")[0]  # the warning section's one example is a detailing one
    assert "slike pre i posle, lepo se vidi" in dm and "keramičku zaštitu" not in dm
    generic = drafter.select_examples(SR_FILE, "Instagram", "frizerski salon")
    assert "pacijent" not in generic.split("## Questions")[0]  # clinic block no longer leaks into every niche
    assert "sviđa mi se kako predstavljate svoje usluge" in generic


# --- prompt ------------------------------------------------------------------------

def test_prompt_is_niche_neutral_and_uses_the_niche_question():
    lead = _lead(language="sr", market="serbia", niche_label="frizerski saloni", industry="hair salon")
    plain = drafter.build_prompt(lead, "Instagram", _outreach(), 0, "")
    for word in ("detailing", "school", "wash"):
        assert word not in plain.lower()
    assert "Questions section" in plain
    niche = Niche(name="hair salon", search_terms=["frizer"], phrase="frizerski saloni",
                  question="da li Vam klijenti zakazuju i preko poruka?")
    asked = drafter.build_prompt(lead, "Instagram", _outreach(), 0, "", niche=niche)
    assert "da li Vam klijenti zakazuju i preko poruka?" in asked and "Questions section" not in asked


def test_english_prompt_uses_english_rules():
    p = drafter.build_prompt(_lead(), "Instagram", _outreach(), 0, "")
    assert "I'm Kristijan" in p and "Vaš" not in p and "Pozdrav" not in p


def _audited(**kw):
    return _lead(domain="smile.ae", lead_type="redesign", site_reachable=True, **kw)


def test_audit_offer_rule_only_on_instagram_when_turned_on():
    on = _outreach(first_offer="audit")
    assert "one-page note" in drafter.build_prompt(_audited(), "Instagram", on, 0, "")
    assert "one-page note" not in drafter.build_prompt(_audited(), "Facebook", on, 0, "")
    assert "one-page note" not in drafter.build_prompt(_audited(), "Instagram", _outreach(), 0, "")


@pytest.mark.parametrize("lead_type, reachable, offer", [
    ("redesign", True, "audit"),
    ("unclear", True, "audit"),          # site live, Lighthouse failed
    ("new_build", None, "question"),     # no site to audit
    ("new_build", False, "question"),    # dead domain
    ("unclear", False, "question"),      # site didn't load
    ("redesign", False, "question"),     # certificate error
    ("redesign", None, "question"),      # not known to have answered (e.g. a Notion page)
])
def test_audit_offer_only_for_leads_with_an_audited_site(lead_type, reachable, offer):
    lead = _lead(domain="smile.ae" if lead_type != "new_build" else "", lead_type=lead_type, site_reachable=reachable)
    assert drafter.offer_for(lead, "Instagram", _outreach(first_offer="audit")) == offer
    assert ("one-page note" in drafter.build_prompt(lead, "Instagram", _outreach(first_offer="audit"), 0, "")) == (
        offer == "audit"
    )


def test_new_build_lead_keeps_question_examples_when_audit_offer_is_on(monkeypatch):
    calls = _fake(monkeypatch, {"message": "Hi, I came across your profile. How do new patients find you?",
                                "verify_note": ""})
    drafter.draft_lead(_lead(lead_type="new_build"), _outreach(first_offer="audit"), _CFG, _market("uae"),
                       examples=EN_FILE)
    assert "with audit offer" not in calls[0]["prompt"] and "one-page note" not in calls[0]["prompt"]


def test_landing_url_replaces_site_link_when_links_are_on():
    niche = Niche(name="dental clinic", search_terms=["dentist"], phrase="dental clinics",
                  landing_url="onixweblab.com/clinics")
    p = drafter.build_prompt(_lead(), "Instagram", _outreach(link_in_first_message=True), 0, "", niche=niche)
    assert "onixweblab.com/clinics" in p


# --- draft_lead: language and LinkedIn ----------------------------------------------

def _fake(monkeypatch, *answers):
    calls = []
    it = iter(answers)

    def fake(prompt, model, timeout_s, **kw):
        calls.append({"prompt": prompt, **kw})
        return next(it)

    monkeypatch.setattr(drafter, "_call_claude", fake)
    return calls


_CFG = {"model": "m", "timeout_s": 5}


def test_linkedin_draft_holds_both_outputs_labelled(monkeypatch):
    lead = _lead(social_links={"linkedin": "https://linkedin.com/company/x"})
    calls = _fake(monkeypatch, {
        "connection_note": "Hi, I came across Smile Dental in Dubai. I build websites for clinics.",
        "message": "Hi, thanks for connecting. I'm Kristijan. How do new patients find you today?",
        "verify_note": "",
    })
    out = drafter.draft_lead(lead, _outreach(), _CFG, _market("uae"), examples=EN_FILE)
    assert out.suggested_channel == "LinkedIn"
    assert out.draft_message == (
        "Connection note:\nHi, I came across Smile Dental in Dubai. I build websites for clinics.\n\n"
        "After accept:\nHi, thanks for connecting. I'm Kristijan. How do new patients find you today?"
    )
    assert "connection_note" in calls[0]["schema"]["required"]
    assert "in English" in calls[0]["system"]


def test_linkedin_note_too_long_is_retried_then_failed(monkeypatch):
    lead = _lead(social_links={"linkedin": "https://linkedin.com/in/x"})
    bad = {"connection_note": "Hi, " + "x" * 400, "message": "Hi, thanks. How do patients find you?", "verify_note": ""}
    calls = _fake(monkeypatch, bad, bad)
    out = drafter.draft_lead(lead, _outreach(), _CFG, _market("uae"))
    assert len(calls) == 2 and "connection note" in calls[1]["prompt"]
    assert out.draft_message == "" and out.errors[-1].startswith("draft: connection note")


def test_uae_lead_never_gets_a_serbian_draft(monkeypatch):
    calls = _fake(monkeypatch, {"message": "Pozdrav, kako Vam stižu upiti?", "verify_note": ""},
                  {"message": "Pozdrav, kako Vam stižu upiti?", "verify_note": ""})
    out = drafter.draft_lead(_lead(), _outreach(), _CFG, _market("uae"))
    assert out.draft_message == "" and "must start with 'Hi'" in out.errors[-1]
    assert calls[0]["schema"] == drafter._SCHEMA


# --- config ---------------------------------------------------------------------

def _config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return load_config(path)


def test_first_offer_and_examples_path_defaults_and_validation(tmp_path):
    outreach = _config(tmp_path, "")["outreach"]
    assert outreach["first_offer"] == "question"
    assert drafter.examples_path(outreach, "en") == "config/outreach.en.md"
    with pytest.raises(ConfigError, match="first_offer must be question or audit"):
        _config(tmp_path, "outreach:\n  first_offer: price\n")
    with pytest.raises(ConfigError, match="must contain {language}"):
        _config(tmp_path, "outreach:\n  examples_path: config/outreach.md\n")


# --- review claims only from market.review_strong ------------------------------------

_VIBER_MSG = (
    "Pozdrav, naišao sam na vašu ordinaciju na Google mapama. Ja sam Kristijan, pravim sajtove za lokalne biznise.\n\n"
    "Primetio sam da imate dosta ocena, pa me zanima da li ste razmišljali o sajtu?"
)


def test_viber_review_claim_needs_review_strong():
    assert validate_message(_VIBER_MSG, "Viber/Whatsapp", SR, reviews_ok=True) == []
    few = validate_message(_VIBER_MSG, "Viber/Whatsapp", SR, reviews_ok=False)
    assert any("rating" in p for p in few)
    recenzije = _VIBER_MSG.replace("dosta ocena", "dosta recenzija")
    assert any("recenzij" in p for p in validate_message(recenzije, "Viber/Whatsapp", SR, reviews_ok=False))
    photos = _VIBER_MSG.replace("dosta ocena", "lepe slike radova")
    assert validate_message(photos, "Viber/Whatsapp", SR, reviews_ok=False) == []
    free = photos.replace("?", ". Da li želite da vam pošaljem primer besplatno?")
    assert validate_message(free, "Viber/Whatsapp", SR, reviews_ok=False) == []  # "besplatno" stays allowed


def _sr_viber_lead(reviews):
    return Lead(company="Kreativ Dental", domain="", source="maps", market="serbia", language="sr",
                industry="dental clinic", rating=4.8, review_count=reviews, contact_phones=["+381641234589"])


def test_viber_prompt_cites_reviews_only_from_review_strong():
    serbia = _market("serbia")  # review_strong 50 (Market default)
    assert serbia.review_strong == 50
    few = drafter.build_prompt(_sr_viber_lead(23), "Viber/Whatsapp", _outreach(), 0, "", market=serbia)
    assert "Do NOT mention Google ratings or reviews" in few and "23 reviews" not in few
    many = drafter.build_prompt(_sr_viber_lead(80), "Viber/Whatsapp", _outreach(), 0, "", market=serbia)
    assert "80 reviews" in many and 'only as "dosta ocena"' in many
    assert "Do NOT mention Google ratings" not in many
    no_market = drafter.build_prompt(_sr_viber_lead(500), "Viber/Whatsapp", _outreach(), 0, "")
    assert "Do NOT mention Google ratings" in no_market  # unknown threshold: never cite


def test_draft_lead_rejects_dosta_ocena_below_review_strong(monkeypatch):
    serbia = _market("serbia")
    calls = _fake(monkeypatch, {"message": _VIBER_MSG, "verify_note": ""}, {"message": _VIBER_MSG, "verify_note": ""})
    out = drafter.draft_lead(_sr_viber_lead(23), _outreach(), _CFG, serbia)
    assert out.suggested_channel == "Viber/Whatsapp"
    assert out.draft_message == "" and "rating" in out.errors[-1] and len(calls) == 2
    _fake(monkeypatch, {"message": _VIBER_MSG, "verify_note": ""})
    assert drafter.draft_lead(_sr_viber_lead(60), _outreach(), _CFG, serbia).draft_message == _VIBER_MSG


# --- approved sender line ------------------------------------------------------------

@pytest.mark.parametrize("message, channel, rules", [
    ("Pozdrav, naišao sam na Vaš profil. Ja sam Kristijan, pravim sajtove za lokalne firme, pa me zanima kako?",
     "Instagram", SR),
    ("Subject: Pitanje\n\nDobar dan, ovde Kristijan, pravim sajtove za lokalne firme. Kako?", "Email", SR),
    (_VIBER_MSG, "Viber/Whatsapp", SR),
    ("Hi, I came across your profile. I'm Kristijan, I build websites for local businesses, so how?", "Instagram", EN),
    ("Hi, I came across your profile. I'm Kristijan, I build websites for clinics, so how?", "Instagram", EN),
    ("Subject: Q\n\nHello, this is Kristijan, I build websites for clinics. How?\n\nKristijan, ONIX Web Lab, onixweblab.com",
     "Email", EN),
])
def test_approved_sender_line_passes(message, channel, rules):
    assert not any("sender line" in p for p in validate_message(message, channel, rules))


@pytest.mark.parametrize("message, channel, rules", [
    ("Pozdrav, naišao sam na Vaš profil. Ja sam Kristijan, pravim sajtove i online zakazivanje za lokalne firme, "
     "pa me zanima kako?", "Instagram", SR),
    ("Pozdrav, naišao sam na Vaš profil. Ja sam Kristijan, pravim sajtove za lokalne firme i online zakazivanje, "
     "pa me zanima kako?", "Instagram", SR),
    (_VIBER_MSG.replace("pravim sajtove za lokalne biznise", "pravim sajtove i online zakazivanje za ordinacije"),
     "Viber/Whatsapp", SR),
    (_VIBER_MSG.replace("lokalne biznise", "lokalne firme"), "Viber/Whatsapp", SR),  # Viber's line is "biznise"
    ("Hi, I came across your profile. I'm Kristijan, I build websites and booking systems for clinics, so how?",
     "Instagram", EN),
    ("Hi, I came across your profile. I'm Kristijan, I build websites for local businesses and clinics, so how?",
     "Instagram", EN),
])
def test_sender_line_with_added_services_is_rejected(message, channel, rules):
    assert any("sender line must be exactly" in p for p in validate_message(message, channel, rules))


def test_prompts_spell_out_the_sender_line():
    sr = drafter.build_prompt(_lead(language="sr", market="serbia"), "Instagram", _outreach(), 0, "")
    assert '"pravim sajtove za lokalne firme"' in sr and "online zakazivanje" in sr  # named as forbidden
    viber = drafter.build_prompt(_sr_viber_lead(10), "Viber/Whatsapp", _outreach(), 0, "")
    assert 'word for word: "pravim sajtove za lokalne biznise"' in viber
    en = drafter.build_prompt(_lead(), "Instagram", _outreach(), 0, "")
    assert "I build websites for local businesses" in en and "I build websites for clinics" in en


def test_audited_lead_gets_audit_offer_examples_when_audit_offer_is_on(monkeypatch):
    calls = _fake(monkeypatch, {"message": "Hi, I came across your profile. Could I send a short note?",
                                "verify_note": ""})
    drafter.draft_lead(_audited(), _outreach(first_offer="audit"), _CFG, _market("uae"), examples=EN_FILE)
    assert "with audit offer" in calls[0]["prompt"].lower()
