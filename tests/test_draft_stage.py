"""Draft stage — the claude CLI call is faked; file handling, eligibility,
retry-on-rule-violation and failure degradation are under test."""

import json

from pipeline.drafting import drafter
from pipeline.runner import main
from pipeline.schema import Lead, read_jsonl, write_jsonl


def _lead(company, domain, **kw):
    kw.setdefault("status", "scored")
    kw.setdefault("qualified", True)
    kw.setdefault("social_links", {"instagram": "https://instagram.com/x"})
    return Lead(company=company, domain=domain, source="osm", **kw)


def _make_run(tmp_path, monkeypatch, leads):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    run_dir = tmp_path / "data" / "runs" / "r"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "scored.jsonl", leads)
    return run_dir


def _fake(monkeypatch, fail_for=(), message="Dobar dan, ja sam Kristijan. Kako Vam stižu upiti?"):
    calls = []

    def fake(prompt, model, timeout_s):
        calls.append(prompt)
        if any(name in prompt for name in fail_for):
            raise RuntimeError("boom")
        return {"message": message, "verify_note": "check the slogan"}

    monkeypatch.setattr(drafter, "_call_claude", fake)
    return calls


def test_only_qualified_scored_undrafted_leads_are_drafted(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, monkeypatch, [
        _lead("A", "a.rs"),
        _lead("B", "b.rs", qualified=False),
        _lead("C", "c.rs", status="synced"),
        _lead("D", "d.rs", draft_message="kept"),
    ])
    _fake(monkeypatch)
    assert main(["draft", "--run", "r"]) == 0
    by = {l.company: l for l in read_jsonl(run_dir / "scored.jsonl")}
    assert by["A"].draft_message.startswith("Dobar dan")
    assert by["A"].suggested_channel == "Instagram"
    assert by["A"].draft_notes == "check the slogan"
    assert by["B"].draft_message == "" and by["C"].draft_message == ""
    assert by["D"].draft_message == "kept"
    counts = json.loads((run_dir / "manifest.json").read_text())["draft"]["counts"]
    assert counts == {"input": 4, "drafted": 1, "failed": 0, "no_contact_data": 0, "skipped": 3}


def test_lead_without_contact_data_is_counted_not_errored(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, monkeypatch, [_lead("Nobody", "n.rs", social_links={})])
    calls = _fake(monkeypatch)
    main(["draft", "--run", "r"])
    lead = read_jsonl(run_dir / "scored.jsonl")[0]
    assert calls == [] and lead.errors == [] and lead.draft_message == ""
    assert json.loads((run_dir / "manifest.json").read_text())["draft"]["counts"]["no_contact_data"] == 1


def test_failure_lands_in_errors_and_does_not_halt(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, monkeypatch, [_lead("Bad", "bad.rs"), _lead("Good", "good.rs")])
    _fake(monkeypatch, fail_for=("Bad",))
    assert main(["draft", "--run", "r"]) == 0
    by = {l.company: l for l in read_jsonl(run_dir / "scored.jsonl")}
    assert by["Bad"].draft_message == "" and by["Bad"].errors == ["draft: boom"]
    assert by["Good"].draft_message


def test_rerun_retries_only_failed_leads(tmp_path, monkeypatch):
    _make_run(tmp_path, monkeypatch, [_lead("Bad", "bad.rs"), _lead("Good", "good.rs")])
    _fake(monkeypatch, fail_for=("Bad",))
    main(["draft", "--run", "r"])
    calls = _fake(monkeypatch)
    main(["draft", "--run", "r"])
    assert len(calls) == 1 and "Bad" in calls[0]


def test_limit_caps_calls(tmp_path, monkeypatch):
    _make_run(tmp_path, monkeypatch, [_lead(f"L{i}", f"l{i}.rs") for i in range(3)])
    calls = _fake(monkeypatch)
    main(["draft", "--run", "r", "--limit", "2"])
    assert len(calls) == 2


def test_rule_violation_retries_once_then_succeeds(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs")])
    answers = iter(["Dobar dan, besplatno!", "Dobar dan. Kako Vam stižu upiti?"])
    prompts = []

    def fake(prompt, model, timeout_s):
        prompts.append(prompt)
        return {"message": next(answers), "verify_note": ""}

    monkeypatch.setattr(drafter, "_call_claude", fake)
    main(["draft", "--run", "r"])
    lead = read_jsonl(run_dir / "scored.jsonl")[0]
    assert lead.draft_message == "Dobar dan. Kako Vam stižu upiti?"
    assert "rejected" in prompts[1] and "banned phrase" in prompts[1]


def test_persistent_violation_is_an_error_not_a_draft(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, monkeypatch, [_lead("A", "a.rs")])
    _fake(monkeypatch, message="Besplatno za Vas!")
    main(["draft", "--run", "r"])
    lead = read_jsonl(run_dir / "scored.jsonl")[0]
    assert lead.draft_message == "" and lead.errors[0].startswith("draft:")


def test_validate_message_rules():
    v = drafter.validate_message
    assert v("Dobar dan. Kako Vam stižu upiti?", "Instagram") == []
    assert any("Cyrillic" in p for p in v("Добар дан", "Instagram"))
    assert any("exclamation" in p for p in v("Zdravo!", "Instagram"))
    assert any("Subject" in p for p in v("Dobar dan", "Email"))
    assert v("Subject: Pitanje\n\nDobar dan", "Email") == []
    assert any("subject" in p for p in v("Subject: x\n\nhej", "Viber/Whatsapp"))
    assert any("limit" in p for p in v("a" * 2000, "Instagram"))


def test_prompt_respects_outreach_settings():
    from pipeline.config import DEFAULTS

    outreach = dict(DEFAULTS["outreach"])
    lead = _lead("Salon Mira", "", industry="hairdresser", lead_type="new_build", site_text="Mira frizer")
    p = drafter.build_prompt(lead, "Instagram", outreach, 0, "EXAMPLES")
    assert "frizerske salone" in p and "no website found" in p.lower()
    assert "Do NOT include any link" in p and "Do NOT name any past client" in p
    assert "EXAMPLES" in p and "Mira frizer" in p
    on = {**outreach, "link_in_first_message": True, "named_references": ["Acme"]}
    p2 = drafter.build_prompt(lead, "Instagram", on, 0, "")
    assert "onixweblab.com" in p2 and "Acme" in p2


def test_email_prompt_allows_only_the_signature_domain():
    from pipeline.config import DEFAULTS

    outreach = dict(DEFAULTS["outreach"])
    lead = _lead("A", "a.rs", contact_emails=["a@a.rs"])
    email = drafter.build_prompt(lead, "Email", outreach, 0, "")
    chat = drafter.build_prompt(lead, "Instagram", outreach, 0, "")
    assert "only exception" in email and outreach["signature"] in email
    assert "only exception" not in chat and "Do NOT include any link" in chat
