"""Out-of-credits handling, the batch / run / go control flow with the automatic
draft step, and draft-notion — stages, Notion and the claude CLI are faked;
control flow and state files are under test."""

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
    kw = {"status": "scored", "qualified": True, "market": "serbia", "language": "sr",
          "social_links": {"instagram": "https://instagram.com/x"}, **kw}
    return Lead(company=company, domain=domain, source="osm", **kw)


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


_ENTRY_NICHE = "    market: serbia\n    niche: {name: dental clinic, search_terms: [dentist], phrase: stomatološke ordinacije}\n"


def _batch(tmp_path, monkeypatch, entries):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    (tmp_path / "b.yaml").write_text(
        "runs:\n" + "".join(f"  - source: maps\n    location: {e}\n" + _ENTRY_NICHE for e in entries)
    )


def _stub_stages(monkeypatch, codes):
    """collect stamps a run id per location; each stage returns codes[(stage, run)] (default 0).
    "draft" is the automatic draft step (runner._auto_draft)."""
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
    for name in ("enrich", "score", "sync"):
        monkeypatch.setattr(runner, f"cmd_{name}", stage(name))
    monkeypatch.setattr(runner, "_auto_draft", stage("draft"))
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("run / batch must never prompt"))
    return log


def test_batch_runs_in_order_and_skips_done_on_rerun(tmp_path, monkeypatch):
    _batch(tmp_path, monkeypatch, ["A", "B"])
    log = _stub_stages(monkeypatch, {})
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 0
    assert [x for x in log if x[0] == "collect"] == [("collect", "A"), ("collect", "B")]
    log.clear()
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 0
    assert log == []  # everything already done


def test_batch_drafts_after_sync_for_every_entry(tmp_path, monkeypatch):
    _batch(tmp_path, monkeypatch, ["A"])
    log = _stub_stages(monkeypatch, {})
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 0
    assert [x[0] for x in log] == ["collect", "enrich", "score", "sync", "draft"]


def test_batch_out_of_credits_finishes_later_entries_without_drafting(tmp_path, monkeypatch, capsys):
    _batch(tmp_path, monkeypatch, ["A", "B"])
    log = _stub_stages(monkeypatch, {("draft", "run-A"): EXIT_OUT_OF_CREDITS})
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == EXIT_OUT_OF_CREDITS
    assert ("sync", "run-B") in log and ("draft", "run-B") not in log  # B still lands in Notion
    state = json.loads((tmp_path / "data" / "batches" / "b.json").read_text())
    assert [v["status"] for v in state.values()] == ["done", "done"]  # no pause, no resume state
    out = capsys.readouterr()
    assert "drafting stopped (out of AI credits)" in out.out and "draft-notion" in out.err

    log.clear()  # re-run: nothing to resume; undrafted pages are draft-notion's job
    assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 0
    assert log == []


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
    assert (seen["location"], seen["source"]) == ("Novi Sad", "maps")
    assert "draft" not in seen  # drafting is automatic in run, not a flag
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
    answers = ["1"] + _LEAD_GEN[1:] + ["osm", "car wash", "amenity=car_wash", "Novi Sad", "y"]
    seen = _go(tmp_path, monkeypatch, answers)
    assert main(["go"]) == 0
    assert seen["source"] == "osm"
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


def test_go_yes_skips_the_start_confirmation(tmp_path, monkeypatch):
    seen = _go(tmp_path, monkeypatch, _LEAD_GEN + ["maps", "Beograd"])  # no answer for Start?
    assert main(["go", "--yes"]) == 0
    assert seen["location"] == "Beograd"


def test_go_has_no_both_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    monkeypatch.setattr("builtins.input", lambda prompt: "both")
    assert main(["go"]) == 1


def test_go_rejects_unknown_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    monkeypatch.setattr("builtins.input", lambda prompt: "manual")
    assert main(["go"]) == 1


def test_go_draft_outreach_writes_without_a_second_confirm(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    answers = iter(["draft", "2", "all"])  # prefix match, redraft, no limit
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []
    monkeypatch.setattr(runner, "cmd_draft_notion", lambda a: calls.append((a.dry_run, a.limit, a.redraft)) or 0)
    assert main(["go"]) == 0
    assert calls == [(False, None, True)]  # auto_draft on: one writing pass


def test_go_draft_outreach_with_auto_draft_off_asks_before_writing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("outreach:\n  auto_draft: false\n")
    answers = iter(["draft", "1", "5", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []
    monkeypatch.setattr(runner, "cmd_draft_notion", lambda a: calls.append((a.yes, a.limit)) or 0)
    assert main(["go"]) == 0
    assert calls == [(False, 5), (True, 5)]


def test_go_single_lead_asks_which_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    answers = iter(["2", "1", "Salon Mira"])  # scope, limit 1, company
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []
    monkeypatch.setattr(runner, "cmd_draft_notion", lambda a: calls.append((a.limit, a.company, a.redraft)) or 0)
    assert main(["go", "--source", "notion"]) == 0
    assert calls == [(1, "Salon Mira", True)]
    answers = iter(["1", "1", "auto"])
    assert main(["go", "--source", "notion"]) == 0
    assert calls[-1] == (1, "", False)


def test_go_source_flag_skips_the_menu(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    answers = iter(["1", "5"])  # scope, limit
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []
    monkeypatch.setattr(runner, "cmd_draft_notion", lambda a: calls.append((a.limit, a.redraft)) or 0)
    assert main(["go", "--source", "notion"]) == 0
    assert calls == [(5, False)]


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


def test_batch_rejects_the_old_draft_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    for key in ("draft: true", "no_draft: true"):
        (tmp_path / "b.yaml").write_text(f"runs:\n  - source: maps\n    location: A\n    {key}\n" + _ENTRY_NICHE)
        assert main(["batch", "--file", "b.yaml", "--pause-s", "0"]) == 1


def test_run_drafts_after_sync_and_never_prompts(monkeypatch):
    log = _stub_stages(monkeypatch, {})
    monkeypatch.setattr(runner, "get_sink", lambda *a: None)
    monkeypatch.setattr(runner, "load_config", lambda c: {})
    base = ["run", "--source", "maps", "--location", "A"]
    assert main(base) == 0
    assert [x[0] for x in log] == ["collect", "enrich", "score", "sync", "draft"]
    with pytest.raises(SystemExit):
        main(base + ["--draft"])  # the old flag is gone


def test_run_reports_out_of_credits_after_sync(monkeypatch):
    log = _stub_stages(monkeypatch, {("draft", "run-A"): EXIT_OUT_OF_CREDITS})
    monkeypatch.setattr(runner, "get_sink", lambda *a: None)
    monkeypatch.setattr(runner, "load_config", lambda c: {})
    assert main(["run", "--source", "maps", "--location", "A"]) == EXIT_OUT_OF_CREDITS
    assert ("sync", "run-A") in log


def test_draft_command_is_gone():
    with pytest.raises(SystemExit):
        main(["draft", "--run", "r"])


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
    cache_path = "data/seen_domains.json"  # absent: no page has a JSONL row

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
        "config": "config.yaml", "limit": None, "yes": False, "dry_run": False, "redraft": False, **extra})


@pytest.fixture
def notion_dir(tmp_path, monkeypatch):
    """draft-notion reads config.yaml and data/runs from the cwd: keep it off the repo's."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    return tmp_path


def test_draft_notion_overwrite_needs_redraft_flag(notion_dir, monkeypatch, capsys):
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


def test_draft_notion_skips_pages_without_market_or_industry(notion_dir, monkeypatch, capsys):
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


def test_draft_notion_dry_run_and_auto_draft_off_write_nothing(notion_dir, monkeypatch, capsys):
    sink = _FakeDraftSink(lambda redraft: [_notion_page()])
    monkeypatch.setattr(runner, "get_sink", lambda *a: sink)
    monkeypatch.setattr(runner, "draft_lead", lambda lead, *a, **k: replace(
        lead, suggested_channel="Instagram", draft_message="Pozdrav, kako Vam stižu upiti?"))
    assert runner.cmd_draft_notion(_draft_notion_args(dry_run=True)) == 0
    assert sink.updates == [] and "would write" in capsys.readouterr().out
    (notion_dir / "config.yaml").write_text(MARKETS_YAML + "outreach:\n  auto_draft: false\n")
    assert runner.cmd_draft_notion(_draft_notion_args()) == 0
    assert sink.updates == [] and "pass --yes to write" in capsys.readouterr().out
    assert runner.cmd_draft_notion(_draft_notion_args(yes=True)) == 0
    assert len(sink.updates) == 1


def test_draft_notion_drafts_a_run_page_from_its_jsonl_row(notion_dir, monkeypatch):
    """Pages a run left undrafted (credits out) keep profile_facts and the niche question."""
    from support import FakeNotion

    from pipeline.sinks.notion_sink import cache_key

    run_dir = notion_dir / "data" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    row = _lead("Zubić", "zubic.rs", status="synced", run_id="r1", tier="A", niche_label="stomatološke ordinacije",
                profile_facts=["Named on the website: Implantologija"])
    write_jsonl(run_dir / "scored.jsonl", [row])
    (run_dir / "manifest.json").write_text(json.dumps({"niche": {
        "name": "dental clinic", "search_terms": ["dentist"], "phrase": "stomatološke ordinacije",
        "question": "da li pacijenti zakazuju preko sajta?"}}))
    notion = FakeNotion(notion_dir / "data" / "seen_domains.json")
    notion.add("p-z", "Zubić", tier="A", **{"SM Audit": {"rich_text": [{"plain_text": "lepe slike"}]}})
    notion.add("p-hand", "Ručno", tier=None,  # hand-made page: no JSONL row, drafted from the page
               **{"Social Links": _notion_page()["properties"]["Social Links"]})
    (notion_dir / "data" / "seen_domains.json").write_text(json.dumps({cache_key(row): {"page_id": "p-z"}}))
    monkeypatch.setattr(runner, "get_sink", lambda *a: notion)
    prompts = []

    def fake(prompt, model, timeout_s, **kw):
        prompts.append(prompt)
        return {"message": "Pozdrav. Kako Vam stižu upiti?", "verify_note": ""}

    monkeypatch.setattr(drafter, "_call_claude", fake)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("never prompts"))
    assert runner.cmd_draft_notion(_draft_notion_args()) == 0
    zubic = next(p for p in prompts if "Business: Zubić" in p)
    assert "Named on the website: Implantologija" in zubic and "da li pacijenti zakazuju preko sajta?" in zubic
    assert "lepe slike" in zubic  # SM Audit from the page still wins
    assert "stomatološke ordinacije" in zubic
    assert len(prompts) == 2 and sorted(pid for pid, _ in notion.updates) == ["p-hand", "p-z"]


def test_draft_notion_out_of_credits_exits_3_and_reports(notion_dir, monkeypatch, capsys):
    pages = [_notion_page(), {**_notion_page(), "id": "p2"}]
    sink = _FakeDraftSink(lambda redraft: pages)
    monkeypatch.setattr(runner, "get_sink", lambda *a: sink)

    def broke(*a, **k):
        raise DraftBudgetError("usage limit reached")

    monkeypatch.setattr(runner, "draft_lead", broke)
    assert runner.cmd_draft_notion(_draft_notion_args()) == EXIT_OUT_OF_CREDITS
    assert sink.updates == [] and "2 pages undrafted" in capsys.readouterr().err


@pytest.mark.parametrize("status", ["scored", "disqualified", "synced"])
def test_draft_notion_matches_rows_of_a_rescored_run(notion_dir, monkeypatch, status):
    """Re-scoring resets "synced" to "scored": the page must still find its row (by cache key,
    or by a shared email like --rescore), so profile_facts and the niche question reach the prompt."""
    from support import FakeNotion

    from pipeline.sinks.notion_sink import cache_key

    run_dir = notion_dir / "data" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    by_key = _lead("Zubić", "zubic.rs", status=status, run_id="r1", profile_facts=["Named on the website: Implantologija"])
    by_email = _lead("Osmeh", "", status=status, run_id="r1", contact_emails=["info@osmeh.rs"],
                     profile_facts=["Google Maps description: Porodična ordinacija"])
    write_jsonl(run_dir / "scored.jsonl", [by_key, by_email, _lead("Old", "old.rs", status="enriched", run_id="r1")])
    (run_dir / "manifest.json").write_text(json.dumps({"niche": {
        "name": "dental clinic", "search_terms": ["dentist"], "phrase": "stomatološke ordinacije",
        "question": "da li pacijenti zakazuju preko sajta?"}}))
    notion = FakeNotion(notion_dir / "data" / "seen_domains.json")
    notion.add("p-z", "Zubić")
    notion.add("p-o", "Osmeh", **{"Social Links": _notion_page()["properties"]["Social Links"]})
    # Osmeh's cache entry is keyed by an earlier domain; only the email links it
    (notion_dir / "data" / "seen_domains.json").write_text(json.dumps({
        cache_key(by_key): {"page_id": "p-z"},
        "domain:osmeh-old.rs": {"page_id": "p-o", "snapshot": {"emails": ["info@osmeh.rs"]}},
    }))
    monkeypatch.setattr(runner, "get_sink", lambda *a: notion)
    prompts = []
    monkeypatch.setattr(drafter, "_call_claude", lambda prompt, *a, **k: prompts.append(prompt) or {
        "message": "Pozdrav. Kako Vam stižu upiti?", "verify_note": ""})
    assert runner.cmd_draft_notion(_draft_notion_args()) == 0
    zubic = next(p for p in prompts if "Business: Zubić" in p)
    osmeh = next(p for p in prompts if "Business: Osmeh" in p)
    assert "Implantologija" in zubic and "da li pacijenti zakazuju preko sajta?" in zubic
    assert "Porodična ordinacija" in osmeh and "da li pacijenti zakazuju preko sajta?" in osmeh
