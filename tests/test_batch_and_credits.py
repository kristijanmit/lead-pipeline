"""Out-of-credits handling in draft, and the batch command's pause/resume —
stages and the claude CLI are faked; control flow and state files are under test."""

import argparse
import json
import subprocess
from dataclasses import replace

import pytest

from pipeline import runner
from pipeline.drafting import DraftBudgetError, drafter
from pipeline.runner import EXIT_OUT_OF_CREDITS, main
from pipeline.schema import Lead, read_jsonl, write_jsonl
from support import MARKETS_YAML


def _lead(company, domain, **kw):
    return Lead(
        company=company, domain=domain, source="osm", status="scored", qualified=True,
        market="serbia", language="sr", social_links={"instagram": "https://instagram.com/x"}, **kw,
    )


def _setup(tmp_path, monkeypatch, leads):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    run_dir = tmp_path / "data" / "runs" / "r"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "scored.jsonl", leads)
    return run_dir


def test_call_claude_maps_limit_text_to_budget_error(monkeypatch):
    proc = subprocess.CompletedProcess([], 1, stdout='{"result":"Claude AI usage limit reached"}', stderr="")
    monkeypatch.setattr(drafter.subprocess, "run", lambda *a, **k: proc)
    with pytest.raises(DraftBudgetError):
        drafter._call_claude("p", "m", 5)


def test_call_claude_other_failure_stays_runtime_error(monkeypatch):
    proc = subprocess.CompletedProcess([], 1, stdout="", stderr="segfault")
    monkeypatch.setattr(drafter.subprocess, "run", lambda *a, **k: proc)
    with pytest.raises(RuntimeError) as e:
        drafter._call_claude("p", "m", 5)
    assert not isinstance(e.value, DraftBudgetError)


def test_draft_stops_on_credits_keeps_finished_and_untouched(tmp_path, monkeypatch, capsys):
    run_dir = _setup(tmp_path, monkeypatch, [_lead("A", "a.rs"), _lead("B", "b.rs"), _lead("C", "c.rs")])
    calls = []

    def fake(prompt, model, timeout_s, **kw):
        calls.append(prompt)
        if len(calls) == 2:
            raise DraftBudgetError("usage limit reached")
        return {"message": "Pozdrav, kako Vam stižu upiti?", "verify_note": ""}

    monkeypatch.setattr(drafter, "_call_claude", fake)
    assert main(["draft", "--run", "r"]) == EXIT_OUT_OF_CREDITS
    by = {l.company: l for l in read_jsonl(run_dir / "scored.jsonl")}
    assert by["A"].draft_message and not by["B"].draft_message and not by["C"].draft_message
    assert by["B"].errors == []  # not marked failed: it simply wasn't attempted
    assert len(calls) == 2  # C was never tried
    assert json.loads((run_dir / "manifest.json").read_text())["draft"]["stopped_out_of_credits"] is True
    assert "no credits" in capsys.readouterr().err


_ENTRY_NICHE = "    market: serbia\n    niche: {name: dental clinic, search_terms: [dentist], phrase: stomatološke ordinacije}\n"


def _batch(tmp_path, monkeypatch, entries):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    (tmp_path / "b.yaml").write_text(
        "runs:\n" + "".join(f"  - source: maps\n    location: {e}\n    draft: true\n" + _ENTRY_NICHE for e in entries)
    )


def _stub_stages(monkeypatch, codes):
    """collect stamps a run id per location; draft returns codes[location] (default 0)."""
    log = []

    def collect(args):
        args.run = f"run-{args.location}"
        log.append(("collect", args.location))
        return 0

    def stage(name):
        def fn(args):
            log.append((name, args.run))
            return codes.get((name, args.run), 0)
        return fn

    monkeypatch.setattr(runner, "cmd_collect", collect)
    for name in ("enrich", "score", "draft", "sync"):
        monkeypatch.setattr(runner, f"cmd_{name}", stage(name))
    return log


def test_batch_runs_in_order_and_skips_done_on_rerun(tmp_path, monkeypatch):
    _batch(tmp_path, monkeypatch, ["A", "B"])
    log = _stub_stages(monkeypatch, {})
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 0
    assert [x for x in log if x[0] == "collect"] == [("collect", "A"), ("collect", "B")]
    log.clear()
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 0
    assert log == []  # everything already done


def test_batch_pauses_on_credits_then_resumes_at_draft(tmp_path, monkeypatch):
    _batch(tmp_path, monkeypatch, ["A", "B"])
    log = _stub_stages(monkeypatch, {("draft", "run-A"): EXIT_OUT_OF_CREDITS})
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 1
    assert ("collect", "B") not in log  # nothing after the pause point started
    state = json.loads((tmp_path / "data" / "batches" / "b.json").read_text())
    assert [v["status"] for v in state.values()] == ["paused"]

    log.clear()
    monkeypatch.setattr(runner, "cmd_draft", lambda a: log.append(("draft", a.run)) or 0)
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 0
    assert log[:2] == [("draft", "run-A"), ("sync", "run-A")]  # resumed, no re-collect of A
    assert ("collect", "A") not in log and ("collect", "B") in log


def test_batch_continues_after_a_failed_entry(tmp_path, monkeypatch):
    _batch(tmp_path, monkeypatch, ["A", "B"])
    log = _stub_stages(monkeypatch, {("enrich", "run-A"): 1})
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 1
    assert ("collect", "B") in log


def test_batch_rejects_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    (tmp_path / "b.yaml").write_text("runs:\n  - source: maps\n    colour: red\n")
    assert main(["batch", "--file", "b.yaml"]) == 1


def _go(tmp_path, monkeypatch, answers):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt: next(it))
    seen = {}
    monkeypatch.setattr(runner, "cmd_run", lambda a: seen.update(vars(a)) or 0)
    return seen


# mode, market, niche name, search terms, phrase, accepted categories, question, landing URL
_LEAD_GEN = ["lead gen", "1", "detailing studio", "detailing, poliranje ,", "auto-detailing studije", "", "", ""]


def test_go_lead_gen_asks_market_niche_source_location(tmp_path, monkeypatch):
    seen = _go(tmp_path, monkeypatch, _LEAD_GEN + ["maps", "Novi Sad", "y"])
    assert main(["go"]) == 0
    assert (seen["location"], seen["source"], seen["draft"]) == ("Novi Sad", "maps", False)
    assert seen["market_def"].id == "serbia" and seen["market_def"].language == "sr"
    niche = seen["niche_def"]
    assert (niche.name, niche.search_terms, niche.phrase) == (
        "detailing studio", ["detailing", "poliranje"], "auto-detailing studije")
    assert niche.osm_tags == []  # only asked for osm


def test_go_search_terms_default_to_the_niche_name(tmp_path, monkeypatch):
    answers = ["lead gen", "2", "dental clinic", "", "dental clinics", "Dentist", "", "", "maps", "Dubai", "y"]
    seen = _go(tmp_path, monkeypatch, answers)
    assert main(["go"]) == 0
    assert seen["market_def"].id == "uae"
    assert seen["niche_def"].search_terms == ["dental clinic"]
    assert seen["niche_def"].accepted_categories == ["Dentist"]


def test_go_osm_asks_for_tags_and_revalidates(tmp_path, monkeypatch, capsys):
    answers = ["3"] + _LEAD_GEN[1:] + ["osm", "car wash", "amenity=car_wash", "Novi Sad", "y"]
    seen = _go(tmp_path, monkeypatch, answers)
    assert main(["go"]) == 0
    assert (seen["source"], seen["draft"]) == ("osm", True)
    assert seen["niche_def"].osm_tags == ["amenity=car_wash"]
    assert "unknown category 'car wash'" in capsys.readouterr().out


def test_go_new_market_prints_yaml_and_is_not_saved(tmp_path, monkeypatch, capsys):
    new_market = ["3", "croatia", "Croatia", "+385", "+3859", "en", "Instagram,Email", "-", "EUR", "hr"]
    seen = _go(tmp_path, monkeypatch, ["lead gen"] + new_market + _LEAD_GEN[2:] + ["maps", "Zagreb", "y"])
    assert main(["go"]) == 0
    market = seen["market_def"]
    assert (market.id, market.country, market.channels) == ("croatia", "Croatia", ["Instagram", "Email"])
    out = capsys.readouterr().out
    assert "paste this into config.yaml" in out and "croatia:" in out
    assert "croatia" not in (tmp_path / "config.yaml").read_text()


def test_go_can_copy_a_niche_from_an_earlier_run(tmp_path, monkeypatch):
    run_dir = tmp_path / "data" / "runs" / "2026-11-01T10-00-00"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"niche": {
        "name": "dental clinic", "search_terms": ["dentist"], "phrase": "stomatološke ordinacije"}}))
    seen = _go(tmp_path, monkeypatch, ["lead gen", "1", "2", "maps", "Niš", "y"])
    assert main(["go"]) == 0
    assert seen["niche_def"].name == "dental clinic"


def test_go_niche_is_never_preselected(tmp_path, monkeypatch, capsys):
    # earlier niches are offered, but an empty answer is re-asked, not defaulted
    run_dir = tmp_path / "data" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"niche": {
        "name": "dental clinic", "search_terms": ["dentist"], "phrase": "x"}}))
    seen = _go(tmp_path, monkeypatch, ["lead gen", "1", "", "1"] + _LEAD_GEN[2:] + ["maps", "Niš", "y"])
    assert main(["go"]) == 0
    assert "required" in capsys.readouterr().out
    assert seen["niche_def"].name == "detailing studio"  # "1" = New niche, typed in


def test_go_cancel_runs_nothing(tmp_path, monkeypatch):
    _go(tmp_path, monkeypatch, _LEAD_GEN + ["maps", "Beograd", "n"])
    monkeypatch.setattr(runner, "cmd_run", lambda a: pytest.fail("should not run"))
    assert main(["go"]) == 0


def test_go_rejects_unknown_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    monkeypatch.setattr("builtins.input", lambda prompt: "manual")
    assert main(["go"]) == 1


def test_go_draft_outreach_asks_scope_and_limit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    answers = iter(["draft", "2", "all", "y"])  # prefix match, redraft, no limit, write
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []
    monkeypatch.setattr(runner, "cmd_draft_notion", lambda a: calls.append((a.yes, a.limit, a.redraft)) or 0)
    assert main(["go"]) == 0
    assert calls == [(False, None, True), (True, None, True)]


def test_go_single_lead_asks_which_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    answers = iter(["2", "1", "Salon Mira", "n"])  # scope, limit 1, company, decline write
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []
    monkeypatch.setattr(runner, "cmd_draft_notion", lambda a: calls.append((a.limit, a.company, a.redraft)) or 0)
    assert main(["go", "--source", "notion"]) == 0
    assert calls == [(1, "Salon Mira", True)]
    answers = iter(["1", "1", "auto", "n"])
    assert main(["go", "--source", "notion"]) == 0
    assert calls[-1] == (1, "", False)


def test_go_source_flag_skips_the_menu(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    answers = iter(["1", "5", "n"])  # scope, limit, decline the write
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []
    monkeypatch.setattr(runner, "cmd_draft_notion", lambda a: calls.append((a.yes, a.limit, a.redraft)) or 0)
    assert main(["go", "--source", "notion"]) == 0
    assert calls == [(False, 5, False)]


def test_batch_entry_needs_market_and_niche(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    (tmp_path / "b.yaml").write_text("runs:\n  - source: maps\n    location: A\n    market: serbia\n")
    assert main(["batch", "--file", "b.yaml"]) == 1
    assert "needs niche" in capsys.readouterr().err


def test_batch_rejects_old_categories_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    (tmp_path / "b.yaml").write_text("runs:\n  - source: maps\n    categories: dentist\n" + _ENTRY_NICHE)
    assert main(["batch", "--file", "b.yaml"]) == 1


def test_batch_validates_every_entry_before_running(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    (tmp_path / "b.yaml").write_text(
        "runs:\n  - source: maps\n    location: A\n" + _ENTRY_NICHE
        + "  - source: osm\n    location: B\n" + _ENTRY_NICHE  # osm without osm_tags
    )
    log = _stub_stages(monkeypatch, {})
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 1
    assert log == []
    assert "run #2" in capsys.readouterr().err


def test_batch_accepts_a_full_market_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    (tmp_path / "b.yaml").write_text(
        "runs:\n  - source: maps\n    location: Zagreb\n"
        "    market: {id: croatia, country: Croatia, dial_code: '+385', mobile_prefixes: ['+3859'],"
        " language: en, channels: [Instagram, Email]}\n"
        "    niche: {name: dental clinic, search_terms: [dentist], phrase: dental clinics}\n"
    )
    seen = []
    monkeypatch.setattr(runner, "cmd_collect", lambda a: seen.append(a) or 1)
    main(["batch", "--file", "b.yaml", "--pause-s", "0"])
    assert seen[0].market_def.country == "Croatia" and seen[0].niche_def.name == "dental clinic"


def test_batch_entry_maps_bbox_and_grid_flags():
    ns = runner._run_args(
        argparse_ns := runner.build_parser().parse_args(["batch", "--file", "x"]),
        {"source": "maps", "bbox": "1,2,3,4", "grid": False},
    )
    assert (ns.bbox, ns.no_grid) == ("1,2,3,4", True)
    assert runner._run_args(argparse_ns, {"source": "maps"}).no_grid is False


def test_batch_skips_draft_unless_entry_asks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    (tmp_path / "b.yaml").write_text("runs:\n  - source: maps\n    location: A\n" + _ENTRY_NICHE)
    log = _stub_stages(monkeypatch, {})
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 0
    assert [x[0] for x in log] == ["collect", "enrich", "score", "sync"]


def test_run_drafts_only_with_flag(monkeypatch):
    log = _stub_stages(monkeypatch, {})
    monkeypatch.setattr(runner, "get_sink", lambda *a: None)
    monkeypatch.setattr(runner, "load_config", lambda c: {})
    base = ["run", "--source", "maps", "--location", "A"]
    assert main(base) == 0
    assert "draft" not in [x[0] for x in log]
    log.clear()
    assert main(base + ["--draft"]) == 0
    assert [x[0] for x in log] == ["collect", "enrich", "score", "draft", "sync"]


def _notion_page(**extra):
    props = {
        "Company": {"title": [{"plain_text": "Salon Mira"}]},
        "Industry": {"select": {"name": "hairdresser"}},
        "Market": {"select": {"name": "serbia"}},
        "Language": {"select": {"name": "sr"}},
        "Social Links": {"rich_text": [{"plain_text": "Instagram", "href": "https://instagram.com/mira"}]},
    }
    props.update(extra)
    return {"id": "p1", "properties": props}


class _FakeDraftSink:
    def __init__(self, pages_for):
        self.pages_for = pages_for  # redraft flag -> pages
        self.queries, self.updates = [], []

    def query_draftable(self, limit=None, redraft=False, min_tier="B", company="", page_ids=None):
        self.queries.append((redraft, min_tier))
        return self.pages_for(redraft)

    def update_page(self, page_id, props):
        self.updates.append(props)


def _draft_notion_args(**extra):
    return argparse.Namespace(**{
        "config": "config.yaml", "limit": None, "yes": True, "redraft": False, **extra})


def test_draft_notion_overwrite_needs_redraft_flag(monkeypatch, capsys):
    page = _notion_page(**{
        "Draft Message": {"rich_text": [{"plain_text": "old draft"}]},
        "Status": {"select": {"name": "Queued"}},
    })
    sink = _FakeDraftSink(lambda redraft: [page] if redraft else [])
    monkeypatch.setattr(runner, "get_sink", lambda *a: sink)
    monkeypatch.setattr(runner, "draft_lead", lambda lead, *a, **k: replace(
        lead, suggested_channel="Instagram", draft_message="Pozdrav, nova poruka?"))

    assert runner.cmd_draft_notion(_draft_notion_args()) == 0 and sink.updates == []
    assert "nothing to draft" in capsys.readouterr().out
    assert runner.cmd_draft_notion(_draft_notion_args(redraft=True)) == 0
    assert sink.queries == [(False, "B"), (True, "B")]  # min tier from outreach.auto_draft_min_tier
    assert sink.updates[0]["Draft Message"]["rich_text"][0]["text"]["content"] == "Pozdrav, nova poruka?"
    assert "Status" not in sink.updates[0]  # already Queued
    assert sink.updates[0]["Draft Notes"] == {"rich_text": []}  # old note cleared on a redraft
    del page["properties"]["Status"]  # blank status counts as Not started
    assert runner.cmd_draft_notion(_draft_notion_args(redraft=True)) == 0
    assert sink.updates[1]["Status"] == {"select": {"name": "Queued"}}
    assert "overwrite" in capsys.readouterr().out


def test_draft_notion_skips_pages_without_market_or_industry(monkeypatch, capsys):
    no_market = _notion_page()
    del no_market["properties"]["Market"]
    no_industry = _notion_page()
    del no_industry["properties"]["Industry"]
    ok = _notion_page()
    sink = _FakeDraftSink(lambda redraft: [no_market, no_industry, ok])
    monkeypatch.setattr(runner, "get_sink", lambda *a: sink)
    drafted = []
    monkeypatch.setattr(runner, "draft_lead", lambda lead, *a, **k: drafted.append(lead) or replace(
        lead, suggested_channel="Instagram", draft_message="Pozdrav, kako Vam stižu upiti?",
        draft_notes="no concrete detail found"))

    # --limit counts drafts, so the skipped pages don't use it up
    assert runner.cmd_draft_notion(_draft_notion_args(limit=1)) == 0
    assert len(drafted) == 1 and drafted[0].market == "serbia" and drafted[0].niche == "hairdresser"
    assert sink.updates[0]["Draft Notes"]["rich_text"][0]["text"]["content"] == "no concrete detail found"
    out = capsys.readouterr().out
    assert "no Market" in out and "no Industry" in out
    assert "2 skipped, not drafted: set Market and Industry on the page" in out
