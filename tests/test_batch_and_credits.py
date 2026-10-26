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


def _lead(company, domain, **kw):
    return Lead(
        company=company, domain=domain, source="osm", status="scored", qualified=True,
        social_links={"instagram": "https://instagram.com/x"}, **kw,
    )


def _setup(tmp_path, monkeypatch, leads):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
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

    def fake(prompt, model, timeout_s):
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


def _batch(tmp_path, monkeypatch, entries):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    (tmp_path / "b.yaml").write_text(
        "runs:\n" + "".join(f"  - source: maps\n    location: {e}\n    draft: true\n" for e in entries)
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


def test_go_prompts_for_location_and_categories(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    answers = iter(["maps", "Novi Sad", "detailing, poliranje ,", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    seen = {}
    monkeypatch.setattr(runner, "cmd_run", lambda a: seen.update(vars(a)) or 0)
    assert main(["go"]) == 0
    assert (seen["location"], seen["categories"], seen["source"]) == ("Novi Sad", "detailing,poliranje", "maps")


def test_go_cancel_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    answers = iter(["maps", "Beograd", "detailing", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(runner, "cmd_run", lambda a: pytest.fail("should not run"))
    assert main(["go"]) == 0


def test_go_rejects_unknown_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    monkeypatch.setattr("builtins.input", lambda prompt: "manual")
    assert main(["go"]) == 1


def test_go_notion_dry_run_then_write_on_yes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    answers = iter(["notion", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    calls = []
    monkeypatch.setattr(runner, "cmd_draft_notion", lambda a: calls.append(a.yes) or 0)
    assert main(["go"]) == 0
    assert calls == [False, True]


def test_batch_entry_maps_bbox_and_grid_flags():
    ns = runner._run_args(
        argparse_ns := runner.build_parser().parse_args(["batch", "--file", "x"]),
        {"source": "maps", "bbox": "1,2,3,4", "grid": False},
    )
    assert (ns.bbox, ns.no_grid) == ("1,2,3,4", True)
    assert runner._run_args(argparse_ns, {"source": "maps"}).no_grid is False


def test_batch_skips_draft_unless_entry_asks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    (tmp_path / "b.yaml").write_text("runs:\n  - source: maps\n    location: A\n")
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


def test_draft_notion_overwrite_needs_redraft_flag(monkeypatch, capsys):
    page = {"id": "p1", "properties": {
        "Company": {"title": [{"plain_text": "Salon Mira"}]},
        "Industry": {"select": {"name": "hairdresser"}},
        "Social Links": {"rich_text": [{"plain_text": "Instagram", "href": "https://instagram.com/mira"}]},
        "SM Audit": {"rich_text": [{"plain_text": "nice photos"}]},
        "Draft Message": {"rich_text": [{"plain_text": "old draft"}]},
        "Status": {"select": {"name": "Queued"}},
    }}
    queries, updates = [], []

    class FakeSink:
        def query_draftable(self, limit, redraft, need_note):
            queries.append((redraft, need_note))
            return [page] if redraft else []

        def update_page(self, page_id, props):
            updates.append(props)

    monkeypatch.setattr(runner, "get_sink", lambda *a: FakeSink())
    monkeypatch.setattr(runner, "draft_lead", lambda lead, *a, **k: replace(
        lead, suggested_channel="Instagram", draft_message="Pozdrav, nova poruka?"))
    def base(**extra):
        return argparse.Namespace(**{
            "config": "config.yaml", "limit": None, "yes": True, "redraft": False, "all": False, **extra})

    assert runner.cmd_draft_notion(base()) == 0 and updates == []
    assert "nothing to draft" in capsys.readouterr().out
    assert runner.cmd_draft_notion(base(redraft=True)) == 0
    assert queries == [(False, True), (True, True)]
    assert updates[0]["Draft Message"]["rich_text"][0]["text"]["content"] == "Pozdrav, nova poruka?"
    assert "Status" not in updates[0]  # already Queued
    assert "overwrite" in capsys.readouterr().out
