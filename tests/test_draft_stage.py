"""Draft stage (script_edits.md §11): the auto step after sync and the drafter.
The claude CLI and Notion are faked (tests/support.FakeNotion); eligibility,
page updates, retry-on-rule-violation, failure degradation and credit-out are
under test."""

import argparse
import json

import pytest

from pipeline import runner
from pipeline.drafting import DraftBudgetError, drafter
from pipeline.runner import EXIT_OUT_OF_CREDITS
from pipeline.schema import Lead, read_jsonl, write_jsonl
from pipeline.sinks.notion_sink import cache_key
from support import MARKETS_YAML, FakeNotion, select, text


def _lead(company, domain, **kw):
    kw.setdefault("status", "synced")
    kw.setdefault("qualified", True)
    kw.setdefault("tier", "B")
    kw.setdefault("market", "serbia")
    kw.setdefault("language", "sr")
    kw.setdefault("run_id", "r")
    kw.setdefault("social_links", {"instagram": "https://instagram.com/x"})
    return Lead(company=company, domain=domain, source="osm", **kw)


def _make_run(tmp_path, monkeypatch, leads, config=MARKETS_YAML, pages=True):
    """A run whose synced leads each have a Notion page (id "p-<company>", Tier =
    lead.tier) in the sync cache. Notion is a FakeNotion; input() must never run."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(config)
    run_dir = tmp_path / "data" / "runs" / "r"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "scored.jsonl", leads)
    notion = FakeNotion(tmp_path / "data" / "seen_domains.json")
    seen = {}
    for lead in leads:
        if pages and lead.status == "synced":
            notion.add(f"p-{lead.company}", lead.company, tier=lead.tier, market=lead.market)
            seen[cache_key(lead)] = {"page_id": f"p-{lead.company}", "snapshot": {}}
    (tmp_path / "data" / "seen_domains.json").write_text(json.dumps(seen))
    monkeypatch.setattr(runner, "get_sink", lambda *a: notion)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("drafting must never prompt"))
    return run_dir, notion


def _draft(limit=None):
    return runner._auto_draft(argparse.Namespace(config="config.yaml", run="r", sink="notion", limit=limit))


def _fake(monkeypatch, fail_for=(), message="Pozdrav, ja sam Kristijan. Kako Vam stižu upiti?"):
    calls = []

    def fake(prompt, model, timeout_s, **kw):
        calls.append(prompt)
        if any(f"Business: {name}\n" in prompt for name in fail_for):
            raise RuntimeError("boom")
        return {"message": message, "verify_note": "check the slogan"}

    monkeypatch.setattr(drafter, "_call_claude", fake)
    return calls


def _counts(run_dir):
    return json.loads((run_dir / "manifest.json").read_text())["draft"]["counts"]


def test_drafts_only_this_runs_synced_pages_at_min_tier(tmp_path, monkeypatch):
    run_dir, notion = _make_run(tmp_path, monkeypatch, [
        _lead("A", "a.rs", tier="A"),
        _lead("B", "b.rs", tier="B"),
        _lead("C", "c.rs", tier="C"),  # below outreach.auto_draft_min_tier (B)
        _lead("D", "d.rs", status="scored"),  # not synced (e.g. beyond --limit)
    ])
    notion.add("p-other", "Other run")  # eligible, but not this run's page
    calls = _fake(monkeypatch)
    assert _draft() == 0
    assert sorted(pid for pid, _ in notion.updates) == ["p-A", "p-B"]
    assert len(calls) == 2
    page = notion.pages["p-A"]
    assert text(page, "Draft Message").startswith("Pozdrav") and select(page, "Status") == "Queued"
    assert select(page, "Suggested Channel") == "Instagram" and text(page, "Draft Notes") == "check the slogan"
    by = {l.company: l for l in read_jsonl(run_dir / "scored.jsonl")}
    assert by["A"].draft_message and not by["C"].draft_message and not by["D"].draft_message
    assert by["A"].status == "synced"
    assert notion.queries[0]["min_tier"] == "B" and notion.queries[0]["page_ids"] == {"p-A", "p-B", "p-C"}
    assert _counts(run_dir)["drafted"] == 2 and _counts(run_dir)["run_pages"] == 3


def test_skip_verdict_and_existing_draft_are_left_alone_no_sm_audit_needed(tmp_path, monkeypatch):
    run_dir, notion = _make_run(tmp_path, monkeypatch, [_lead("Go", "go.rs"), _lead("Skip", "s.rs"), _lead("Done", "d.rs")])
    notion.pages["p-Skip"]["Verdict"] = {"select": {"name": "Skip"}}
    notion.pages["p-Done"]["Draft Message"] = {"rich_text": [{"plain_text": "kept"}]}
    _fake(monkeypatch)
    assert _draft() == 0
    assert [pid for pid, _ in notion.updates] == ["p-Go"]  # no Verdict, no SM Audit: still drafted
    assert text(notion.pages["p-Done"], "Draft Message") == "kept"


def test_auto_draft_off_drafts_nothing(tmp_path, monkeypatch, capsys):
    _, notion = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs")],
                          config=MARKETS_YAML + "outreach:\n  auto_draft: false\n")
    calls = _fake(monkeypatch)
    assert _draft() == 0
    assert calls == [] and notion.updates == []
    assert "auto_draft is off" in capsys.readouterr().out


def test_page_sm_audit_is_the_first_praise_source(tmp_path, monkeypatch):
    _, notion = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs", profile_facts=["Website title: Zubić"])])
    notion.pages["p-A"]["SM Audit"] = {"rich_text": [{"plain_text": "lepe slike radova"}]}
    calls = _fake(monkeypatch)
    _draft()
    assert "lepe slike radova" in calls[0] and "first-hand and trusted" in calls[0]
    assert "Website title: Zubić" in calls[0]


def test_lead_without_market_is_skipped_not_guessed(tmp_path, monkeypatch, capsys):
    run_dir, notion = _make_run(tmp_path, monkeypatch, [
        _lead("Old", "old.rs", market="", language=""),
        _lead("New", "new.rs"),
    ])
    calls = _fake(monkeypatch)
    assert _draft() == 0
    assert [pid for pid, _ in notion.updates] == ["p-New"] and len(calls) == 1
    assert _counts(run_dir)["skipped"] == 1
    assert "1 skipped, not drafted: no market" in capsys.readouterr().out


def test_english_market_lead_is_drafted_with_english_rules(tmp_path, monkeypatch):
    run_dir, notion = _make_run(tmp_path, monkeypatch, [
        _lead("Dubai Smile", "smile.ae", market="uae", language="en"),
        _lead("Beograd", "bg.rs", market="serbia", language="sr"),
    ])
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "outreach.en.md").write_text("## Instagram (DM)\n### generic\nEN-EXAMPLE\n")
    (tmp_path / "config" / "outreach.sr.md").write_text("## Instagram (DM)\n### generic\nSR-EXAMPLE\n")
    calls = []

    def fake(prompt, model, timeout_s, **kw):
        calls.append((prompt, kw["system"]))
        if "in English" in kw["system"]:
            return {"message": "Hi, I'm Kristijan. How do new patients find you?", "verify_note": ""}
        return {"message": "Pozdrav, ja sam Kristijan. Kako Vam stižu upiti?", "verify_note": ""}

    monkeypatch.setattr(drafter, "_call_claude", fake)
    assert _draft() == 0
    assert text(notion.pages["p-Dubai Smile"], "Draft Message").startswith("Hi")
    assert text(notion.pages["p-Beograd"], "Draft Message").startswith("Pozdrav")
    en_prompt = next(p for p, sys in calls if "Dubai Smile" in p)
    sr_prompt = next(p for p, sys in calls if "Beograd" in p)
    assert "EN-EXAMPLE" in en_prompt and "SR-EXAMPLE" not in en_prompt
    assert "SR-EXAMPLE" in sr_prompt and "Vaš" in sr_prompt and "Vaš" not in en_prompt


def test_missing_examples_file_warns_and_drafts(tmp_path, monkeypatch, capsys):
    _, notion = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs")])
    _fake(monkeypatch)
    assert _draft() == 0
    assert notion.updates
    assert "no examples file config/outreach.sr.md" in capsys.readouterr().err


def test_run_niche_question_reaches_the_prompt(tmp_path, monkeypatch):
    run_dir, _ = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs")])
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "r", "niche": {
        "name": "dental clinic", "search_terms": ["dentist"], "phrase": "stomatološke ordinacije",
        "question": "da li pacijenti zakazuju i preko sajta?",
    }}))
    calls = _fake(monkeypatch)
    _draft()
    assert "da li pacijenti zakazuju i preko sajta?" in calls[0]


def test_draft_lead_refuses_a_language_without_rules():
    import dataclasses

    import pytest
    import yaml

    from pipeline.market import Market

    uae = Market.from_dict("uae", yaml.safe_load(MARKETS_YAML)["markets"]["uae"])
    assert drafter.language_skip_reason(uae) == ""
    german = dataclasses.replace(uae, language="de")
    assert drafter.language_skip_reason(german) == "no drafting rules for language 'de'"
    with pytest.raises(ValueError, match="no drafting rules"):
        drafter.draft_lead(_lead("A", "a.ae"), {}, {}, german)


def test_unknown_market_is_skipped_with_its_reason(tmp_path, monkeypatch, capsys):
    run_dir, _ = _make_run(tmp_path, monkeypatch, [_lead("Zagreb", "z.hr", market="croatia")])
    calls = _fake(monkeypatch)
    assert _draft() == 0
    assert calls == []
    assert "unknown market 'croatia'" in capsys.readouterr().out
    assert _counts(run_dir)["skipped"] == 1


def test_lead_without_contact_data_is_counted_not_errored(tmp_path, monkeypatch):
    run_dir, notion = _make_run(tmp_path, monkeypatch, [_lead("Nobody", "n.rs", social_links={})])
    calls = _fake(monkeypatch)
    _draft()
    assert calls == [] and notion.updates == []
    assert read_jsonl(run_dir / "scored.jsonl")[0].errors == []
    assert _counts(run_dir)["no_contact_data"] == 1


def test_cli_error_is_noted_on_the_page_and_does_not_halt(tmp_path, monkeypatch):
    run_dir, notion = _make_run(tmp_path, monkeypatch, [_lead("Bad", "bad.rs"), _lead("Good", "good.rs")])
    _fake(monkeypatch, fail_for=("Bad",))
    assert _draft() == 0
    bad = notion.pages["p-Bad"]
    assert select(bad, "Status") == "Not started" and not text(bad, "Draft Message")  # stays eligible
    assert "Draft error" in text(bad, "Draft Notes") and "boom" in text(bad, "Draft Notes")
    assert text(notion.pages["p-Good"], "Draft Message")
    by = {l.company: l for l in read_jsonl(run_dir / "scored.jsonl")}
    assert by["Bad"].errors == ["draft: boom"]
    assert _counts(run_dir)["error"] == 1

    calls = _fake(monkeypatch)  # a second pass retries only the page left without a draft
    _draft()
    assert len(calls) == 1 and "Business: Bad" in calls[0]


def test_limit_caps_calls(tmp_path, monkeypatch):
    _make_run(tmp_path, monkeypatch, [_lead(f"L{i}", f"l{i}.rs") for i in range(3)])
    calls = _fake(monkeypatch)
    _draft(limit=2)
    assert len(calls) == 2


def test_rule_violation_retries_once_then_succeeds(tmp_path, monkeypatch):
    _, notion = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs")])
    answers = iter(["Pozdrav, besplatno?", "Pozdrav. Kako Vam stižu upiti?"])
    prompts = []

    def fake(prompt, model, timeout_s, **kw):
        prompts.append(prompt)
        return {"message": next(answers), "verify_note": ""}

    monkeypatch.setattr(drafter, "_call_claude", fake)
    _draft()
    assert text(notion.pages["p-A"], "Draft Message") == "Pozdrav. Kako Vam stižu upiti?"
    assert "rejected" in prompts[1] and "banned phrase" in prompts[1]


def test_persistent_violation_marks_draft_failed_and_continues(tmp_path, monkeypatch):
    run_dir, notion = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs"), _lead("B", "b.rs")])
    answers = iter(["Besplatno za Vas!", "Besplatno za Vas!", "Pozdrav. Kako Vam stižu upiti?"])
    monkeypatch.setattr(drafter, "_call_claude", lambda *a, **k: {"message": next(answers), "verify_note": ""})
    assert _draft() == 0
    a = notion.pages["p-A"]
    assert select(a, "Status") == "Draft failed" and not text(a, "Draft Message")
    assert text(a, "Draft Notes").startswith("Draft failed validation:")
    assert text(notion.pages["p-B"], "Draft Message")  # the next page is still drafted
    lead = read_jsonl(run_dir / "scored.jsonl")[0]
    assert lead.draft_message == "" and lead.errors[0].startswith("draft: invalid:")
    assert _counts(run_dir)["draft_failed"] == 1


def test_out_of_credits_stops_leaves_rest_for_the_next_call(tmp_path, monkeypatch, capsys):
    run_dir, notion = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs"), _lead("B", "b.rs"), _lead("C", "c.rs")])
    calls = []

    def fake(prompt, model, timeout_s, **kw):
        calls.append(prompt)
        if len(calls) == 2:
            raise DraftBudgetError("usage limit reached")
        return {"message": "Pozdrav, kako Vam stižu upiti?", "verify_note": ""}

    monkeypatch.setattr(drafter, "_call_claude", fake)
    assert _draft() == EXIT_OUT_OF_CREDITS
    assert [pid for pid, _ in notion.updates] == ["p-A"]  # B and C untouched, still eligible
    assert len(calls) == 2  # C never tried
    by = {l.company: l for l in read_jsonl(run_dir / "scored.jsonl")}
    assert by["B"].errors == [] and not by["B"].draft_message
    manifest = json.loads((run_dir / "manifest.json").read_text())["draft"]
    assert manifest["stopped_out_of_credits"] is True and manifest["counts"]["undrafted_out_of_credits"] == 2
    assert "2 pages undrafted" in capsys.readouterr().err

    calls2 = _fake(monkeypatch)  # credits back: the next call drafts exactly the rest
    assert _draft() == 0
    assert len(calls2) == 2 and sorted(pid for pid, _ in notion.updates[1:]) == ["p-B", "p-C"]


def test_validate_message_rules():
    from pipeline.drafting.rules_sr import RULES

    def v(message, channel):
        return drafter.validate_message(message, channel, RULES)

    assert v("Pozdrav. Kako Vam stižu upiti?", "Instagram") == []
    assert any("Cyrillic" in p for p in v("Добар дан", "Instagram"))
    assert any("exclamation" in p for p in v("Zdravo!", "Instagram"))
    assert any("Subject" in p for p in v("Dobar dan, pitanje?", "Email"))
    assert v("Subject: Pitanje\n\nDobar dan, pitanje?", "Email") == []
    assert any("Dobar dan" in p for p in v("Pozdrav, pitanje?", "Call"))
    assert any("Pozdrav" in p for p in v("Dobar dan, pitanje?", "Instagram"))
    assert any("question" in p for p in v("Pozdrav, ja sam Kristijan.", "Instagram"))
    assert any("rating" in p for p in v("Pozdrav, imate dobru ocenu. Pitanje?", "Instagram"))
    assert not any("rating" in p for p in v("Pozdrav, procena preko slika. Pitanje?", "Instagram"))
    assert any("banned" in p for p in v("Pozdrav, nisam uspeo da pronađem sajt. Pitanje?", "Instagram"))
    assert any("banned" in p for p in v("Pozdrav, 5 zvezdica. Pitanje?", "Instagram"))
    assert any("emoji" in p for p in v("Pozdrav 🙂 Pitanje?", "Instagram"))
    assert any("subject" in p for p in v("Subject: x\n\nhej", "Viber/Whatsapp"))
    offer = "Pozdrav, imate dosta ocena. Da li želite primer sajta besplatno?"
    assert v(offer, "Viber/Whatsapp") == []  # the Viber format offers a free example and cites reviews
    assert any("banned" in p for p in v(offer, "Instagram")) and any("rating" in p for p in v(offer, "Instagram"))
    assert any("limit" in p for p in v("a" * 2000, "Instagram"))


def test_prompt_respects_outreach_settings():
    from pipeline.config import DEFAULTS

    outreach = dict(DEFAULTS["outreach"])
    lead = _lead("Salon Mira", "", industry="hairdresser", niche_label="frizerske salone",
                 lead_type="new_build", site_text="Mira frizer")
    p = drafter.build_prompt(lead, "Instagram", outreach, 0, "EXAMPLES")
    assert "frizerske salone" in p and "rating" in p.lower()
    assert "Do NOT include any link" in p and "Do NOT name any past client" in p
    assert "EXAMPLES" in p and "Mira frizer" in p
    on = {**outreach, "link_in_first_message": True, "named_references": ["Acme"]}
    p2 = drafter.build_prompt(lead, "Instagram", on, 0, "")
    assert "onixweblab.com" in p2 and "Acme" in p2


def test_prompt_niche_phrase_falls_back_to_industry():
    # Notion pages carry no niche_label until v2 step 6: the niche name is used
    from pipeline.config import DEFAULTS

    lead = _lead("Salon Mira", "", industry="hair salon", lead_type="new_build")
    assert "Niche: hair salon" in drafter.build_prompt(lead, "Instagram", dict(DEFAULTS["outreach"]), 0, "")


def test_email_prompt_allows_only_the_signature_domain():
    from pipeline.config import DEFAULTS

    outreach = dict(DEFAULTS["outreach"])
    lead = _lead("A", "a.rs", contact_emails=["a@a.rs"])
    email = drafter.build_prompt(lead, "Email", outreach, 0, "")
    chat = drafter.build_prompt(lead, "Instagram", outreach, 0, "")
    assert "only exception" in email and outreach["signature"] in email
    assert "only exception" not in chat and "Do NOT include any link" in chat


def test_prompt_includes_operator_sm_audit():
    from pipeline.config import DEFAULTS

    lead = _lead("Salon Mira", "", lead_type="new_build", sm_audit="posts daily, no booking link in bio")
    p = drafter.build_prompt(lead, "Instagram", dict(DEFAULTS["outreach"]), 0, "")
    assert "no booking link in bio" in p and "first-hand and trusted" in p
    assert "first-hand" not in drafter.build_prompt(_lead("B", ""), "Instagram", dict(DEFAULTS["outreach"]), 0, "")


_EXAMPLES = """# Intro

## Instagram (DM)
### detailing | car detailing
DETAIL-IG
### generic
GENERIC-IG

## Facebook
### generic
GENERIC-FB

## SMS / call
CALL-EX

## Questions by business type
QUESTIONS
"""


def test_select_examples_filters_by_channel_and_niche():
    sel = drafter.select_examples
    ig = sel(_EXAMPLES, "Instagram", "car detailing")
    assert "DETAIL-IG" in ig and "GENERIC-IG" not in ig and "GENERIC-FB" not in ig
    assert "QUESTIONS" in ig and "# Intro" in ig
    other = sel(_EXAMPLES, "Instagram", "hairdresser")  # no niche hit -> generic only
    assert "GENERIC-IG" in other and "DETAIL-IG" not in other
    assert "GENERIC-IG" in sel(_EXAMPLES, "Viber/Whatsapp", "x")  # reads the DM section
    viber = _EXAMPLES + "\n## Viber/Whatsapp\n### auto | servis\nVIBER-AUTO\n### generic\nVIBER-GEN\n"
    vb = sel(viber, "Viber/Whatsapp", "auto servis")  # own section wins; no IG sections, no questions
    assert "VIBER-AUTO" in vb and "VIBER-GEN" not in vb and "IG" not in vb and "QUESTIONS" not in vb
    call = sel(_EXAMPLES, "Call", "x")
    assert "CALL-EX" in call and "IG" not in call
    assert sel("plain text, no headings", "Instagram", "x") == "plain text, no headings"
    assert sel(_EXAMPLES, "Email", "x") == _EXAMPLES  # no Email section -> everything


def test_viber_prompt_uses_its_own_rules():
    from pipeline.config import DEFAULTS

    outreach = dict(DEFAULTS["outreach"])
    lead = _lead("Servis Pera", "", industry="auto", rating=4.8, review_count=120)
    viber = drafter.build_prompt(lead, "Viber/Whatsapp", outreach, 0, "EX")
    assert "na Google mapama" in viber and "besplatno" in viber and "Do NOT include any link" not in viber
    insta = drafter.build_prompt(lead, "Instagram", outreach, 0, "EX")
    assert "na Google mapama" not in insta and "Do NOT include any link" in insta


def test_viber_redesign_picks_its_own_examples_and_rules():
    from pipeline.config import DEFAULTS

    text = "## Viber/Whatsapp\n### auto\nNEW-BUILD\n## Viber/Whatsapp (redesign)\n### auto\nREDESIGN\n"
    sel = drafter.select_examples
    assert "REDESIGN" in sel(text, "Viber/Whatsapp", "auto", redesign=True) and "NEW-BUILD" not in sel(text, "Viber/Whatsapp", "auto", redesign=True)
    assert "NEW-BUILD" in sel(text, "Viber/Whatsapp", "auto") and "REDESIGN" not in sel(text, "Viber/Whatsapp", "auto")
    only_new = "## Viber/Whatsapp\n### auto\nNEW-BUILD\n"
    assert "NEW-BUILD" in sel(only_new, "Viber/Whatsapp", "auto", redesign=True)  # falls back

    outreach = dict(DEFAULTS["outreach"])
    with_site = drafter.build_prompt(_lead("Servis Pera", "pera.rs", industry="auto", location="Niš"), "Viber/Whatsapp", outreach, 0, "EX")
    assert "naišao sam na vaš sajt" in with_site and "u Niš" in with_site and "novi sajt" in with_site
    no_site = drafter.build_prompt(_lead("Servis Pera", "", industry="auto"), "Viber/Whatsapp", outreach, 0, "EX")
    assert "na Google mapama" in no_site and "naišao sam na vaš sajt" not in no_site


def test_prompt_without_sm_audit_uses_profile_facts():
    from pipeline.config import DEFAULTS

    lead = _lead("Zubić", "zubic.rs", lead_type="redesign",
                 profile_facts=["Named on the website: Implantologija", "Google Maps category: Dentist"])
    p = drafter.build_prompt(lead, "Instagram", dict(DEFAULTS["outreach"]), 0, "")
    assert "  - Named on the website: Implantologija" in p and "  - Google Maps category: Dentist" in p
    assert "take the one praise point from the profile facts" in p
    assert "first-hand" not in p


def test_prompt_with_no_facts_has_the_plain_praise_rule():
    from pipeline.config import DEFAULTS

    p = drafter.build_prompt(_lead("Nepoznato", "", lead_type="new_build"), "Instagram", dict(DEFAULTS["outreach"]), 0, "")
    assert "Profile facts" in p and "\n  none\n" in p
    assert 'use the plain "sviđa mi se kako radite"' in p and '"no concrete detail found" in verify_note' in p
    en = _lead("Smile", "", market="uae", language="en", lead_type="new_build")
    assert 'use the plain "I like the way you present your services"' in drafter.build_prompt(
        en, "Instagram", dict(DEFAULTS["outreach"]), 0, "")


def test_prompt_rebuilds_facts_for_leads_enriched_before_v14():
    from pipeline.config import DEFAULTS

    old = _lead("Zubić", "", maps_category="Dental clinic", maps_description="Porodična ordinacija od 1998.")
    assert old.profile_facts == []
    p = drafter.build_prompt(old, "Instagram", dict(DEFAULTS["outreach"]), 0, "")
    assert "Google Maps description: Porodična ordinacija od 1998." in p
