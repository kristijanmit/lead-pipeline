"""score-report (v2 step 10): page classification, channel from Communication,
groups, Skip Reason counts, the tier trend, the A/B rule, and the command end to
end with a fake Notion and real JSONL rows. No network."""

import argparse
import json

import pytest

from pipeline import runner
from pipeline.reports.score_report import (
    Group, Settings, ab_rule, format_report, group_by, page_row, page_state, sent_channel,
    skip_reason_counts, tier_trend,
)
from pipeline.schema import Lead, write_jsonl
from pipeline.sinks.notion_sink import cache_key
from support import MARKETS_YAML, FakeNotion


def sel(name):
    return {"select": {"name": name}}


def txt(content):
    return {"rich_text": [{"plain_text": content}]}


def date(day):
    return {"date": {"start": day}}


def page(page_id="p", **props):
    """Notion page; keyword names use _ for spaces (Last_Contacted -> "Last Contacted")."""
    return {"id": page_id, "properties": {k.replace("_", " "): v for k, v in props.items()}}


def state(**props):
    return page_state(page(**props)["properties"])


# --- page state ---------------------------------------------------------------------

@pytest.mark.parametrize("status", ["Sent", "Follow-up due", "Replied", "Closed"])
def test_sent_statuses(status):
    assert state(Status=sel(status)) == "sent"


@pytest.mark.parametrize("props", [
    {"Last_Contacted": date("2026-11-20")},
    {"Communication": txt("2026-11-20 · SENT · Instagram · Pozdrav")},
    {"Outcome": sel("no_reply")},
])
def test_hard_sent_signals_without_status(props):
    assert state(Status=sel("Queued"), **props) == "sent"


def test_skip_after_sending_counts_as_sent():
    assert state(Verdict=sel("Skip"), Last_Contacted=date("2026-11-20")) == "sent"
    assert state(Verdict=sel("Skip"), Status=sel("Sent")) == "sent"


def test_skip_without_send_is_skipped():
    assert state(Verdict=sel("Skip"), Status=sel("Queued"), Draft_Message=txt("x")) == "skipped"


@pytest.mark.parametrize("props", [
    {"Draft_Message": txt("Pozdrav…"), "Status": sel("Not started")},
    {"Status": sel("Queued")},
    {"Status": sel("Draft failed")},
])
def test_pending(props):
    assert state(**props) == "pending"


def test_not_drafted_is_left_out():
    assert state(Status=sel("Not started")) is None
    assert state() is None


def test_dead_alone_is_left_out():
    assert state(Status=sel("Dead")) is None
    # a stale draft doesn't make a Dead page pending either
    assert state(Status=sel("Dead"), Draft_Message=txt("Pozdrav…")) is None
    assert page_row(page(Status=sel("Dead"))) is None


@pytest.mark.parametrize("props", [
    {"Last_Contacted": date("2026-11-01")},
    {"Communication": txt("2026-11-01 · SENT · Email · Hello")},
    {"Outcome": sel("no_reply")},
])
def test_dead_with_another_sent_signal_is_sent(props):
    assert state(Status=sel("Dead"), **props) == "sent"


def test_dead_with_skip_is_skipped():
    assert state(Status=sel("Dead"), Verdict=sel("Skip")) == "skipped"


# --- channel / replies --------------------------------------------------------------

def test_channel_from_latest_sent_line():
    log = (
        "2026-11-10 · SENT · Instagram · Pozdrav\n"
        "2026-11-12 · REPLY · hvala\n"
        "2026-11-15 · SENT · email · follow-up\n"
    )
    assert sent_channel(log) == "Email"  # normalized case
    # by date, not position
    assert sent_channel("2026-11-15 · SENT · Facebook · b\n2026-11-10 · SENT · Instagram · a") == "Facebook"
    assert sent_channel("2026-11-10 · REPLY · hi") == ""


def test_channel_falls_back_to_suggested_channel():
    row = page_row(page(Status=sel("Sent"), Suggested_Channel=sel("Viber/Whatsapp")))
    assert row.channel == "Viber/Whatsapp" and row.channel_fallback
    row = page_row(page(
        Suggested_Channel=sel("Email"), Communication=txt("2026-11-10 · SENT · Instagram DM · hi"),
    ))
    assert row.channel == "Instagram" and not row.channel_fallback


@pytest.mark.parametrize("props,replied,meeting", [
    ({"Outcome": sel("no_reply")}, False, False),
    ({"Outcome": sel("declined")}, True, False),
    ({"Outcome": sel("meeting")}, True, True),
    ({"Outcome": sel("won")}, True, True),
    ({"Status": sel("Replied")}, True, False),
    ({"Communication": txt("2026-11-10 · SENT · Email · a\n2026-11-11 · REPLY · ok")}, True, False),
    ({"Status": sel("Sent")}, False, False),
])
def test_reply_and_meeting(props, replied, meeting):
    row = page_row(page(**props))
    assert (row.replied, row.meeting) == (replied, meeting)


def test_page_fields_win_jsonl_fills_gaps():
    lead = Lead(company="A", domain="a.rs", source="osm", tier="C", market="uae", niche="spa",
                lead_type="redesign", score_confidence="low")
    row = page_row(page(Status=sel("Queued"), Tier=sel("A"), Market=sel("Serbia")), lead)
    assert (row.tier, row.market, row.niche, row.lead_type, row.confidence) == (
        "A", "serbia", "spa", "redesign", "low")
    row = page_row(page(Status=sel("Queued")))
    assert row.tier == row.confidence == "(unknown)"


# --- groups and rules ---------------------------------------------------------------

def _rows(spec):
    """spec: list of (tier, state, extra props) -> PageRows."""
    out = []
    for i, (tier, st, extra) in enumerate(spec):
        props = {"Tier": sel(tier), **extra}
        if st == "sent":
            props.setdefault("Status", sel("Sent"))
        elif st == "skipped":
            props["Verdict"] = sel("Skip")
        else:
            props.setdefault("Status", sel("Queued"))
        out.append(page_row(page(f"p{i}", **props)))
    return out


def test_group_counts_and_rates():
    rows = _rows([("B", "sent", {}), ("A", "sent", {}), ("A", "skipped", {}), ("A", "pending", {})])
    a, b = group_by(rows, "tier")
    assert (a.value, a.sent, a.skipped, a.pending) == ("A", 1, 1, 1)
    assert a.send_rate == 0.5 and a.skip_rate == 0.5  # pending left out of rates
    assert b.value == "B" and b.skip_rate == 0.0


def test_skip_reason_counts():
    rows = _rows([
        ("A", "skipped", {"Skip_Reason": sel("chain")}),
        ("B", "skipped", {"Skip_Reason": sel("chain")}),
        ("B", "skipped", {}),
        ("B", "sent", {"Skip_Reason": sel("closed")}),  # sent: its reason isn't a skip
    ])
    assert skip_reason_counts(rows) == [("chain", 2), ("(none)", 1)]


def test_tier_trend():
    def g(tier, sent, skipped):
        return Group(tier, sent=sent, skipped=skipped)

    assert tier_trend([g("A", 25, 5), g("B", 20, 10), g("C", 10, 20)], 30).startswith("healthy")
    assert tier_trend([g("A", 10, 20), g("B", 25, 5)], 30).startswith("skip-rate doesn't rise")
    assert tier_trend([g("A", 25, 5), g("B", 5, 5)], 30).startswith("not enough data")


S = Settings(min_band=30, ab_min_replies=8, ab_ratio=2.0)


def test_ab_rule_win():
    arms = [Group("uae", sent=40, replied=4), Group("serbia", sent=40, replied=10)]
    assert ab_rule(arms, S).startswith("serbia wins")


def test_ab_rule_not_enough_replies():
    arms = [Group("serbia", sent=40, replied=6), Group("uae", sent=40, replied=1)]
    assert ab_rule(arms, S).startswith("not enough data")


def test_ab_rule_small_arm():
    arms = [Group("serbia", sent=40, replied=10), Group("uae", sent=10, replied=1)]
    out = ab_rule(arms, S)
    assert out.startswith("not enough data") and "uae" in out.split(";")[-1]


def test_ab_rule_no_clear_difference():
    arms = [Group("serbia", sent=40, replied=10), Group("uae", sent=40, replied=7)]
    assert ab_rule(arms, S).startswith("no clear difference")


def test_ab_rule_single_or_no_arm():
    assert ab_rule([Group("serbia", sent=40, replied=10), Group("uae", skipped=5)], S).startswith("only one arm")
    assert ab_rule([], S) == "no sends yet"


def test_format_report_sections_and_small_sample_flags():
    rows = _rows([
        ("A", "sent", {"Communication": txt("2026-11-10 · SENT · Instagram · a"), "Market": sel("serbia")}),
        ("B", "sent", {"Suggested_Channel": sel("Email"), "Market": sel("uae")}),
        ("C", "skipped", {"Skip_Reason": sel("chain")}),
    ])
    out = format_report(rows, S, left_out=4)
    assert "2 sent · 1 skipped · 0 pending" in out and "4 pages left out" in out
    assert "1 of 2 sent pages had no SENT line and used Suggested Channel" in out
    assert "chain" in out
    assert "Markets: not enough data" in out
    assert out.count("*") >= 5  # every group is under 30


# --- command end to end ---------------------------------------------------------------

def test_cmd_score_report(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    lead = Lead(company="A", domain="a.rs", source="osm", status="synced", tier="A", market="serbia",
                niche="dental clinic", score_confidence="low", run_id="r2")
    older = Lead(company="A", domain="a.rs", source="osm", status="synced", score_confidence="high", run_id="r1")
    for run, row in (("r1", older), ("r2", lead)):
        (tmp_path / "data" / "runs" / run).mkdir(parents=True)
        write_jsonl(tmp_path / "data" / "runs" / run / "scored.jsonl", [row])
    (tmp_path / "data" / "seen_domains.json").write_text(
        json.dumps({cache_key(lead): {"page_id": "p-A", "snapshot": {}}}))
    notion = FakeNotion(tmp_path / "data" / "seen_domains.json")
    notion.add("p-A", "A", tier="A", status="Sent", market="serbia")
    notion.add("p-B", "B", tier="B", status="Queued", market="uae", industry="spa")
    notion.add("p-C", "C", tier="B", status="Dead")  # left out
    monkeypatch.setattr(runner, "get_sink", lambda *a: notion)

    args = argparse.Namespace(config="config.yaml", market=None, niche=None, min_band=None)
    assert runner.cmd_score_report(args) == 0
    out = capsys.readouterr().out
    assert "1 sent · 0 skipped · 1 pending" in out and "1 pages left out" in out
    confidence = out.split(" Confidence")[1].split("\n\n")[0]
    assert "low" in confidence and "high" not in confidence  # latest run's row wins
    assert notion.updates == []  # read only

    args.market = "UAE"
    assert runner.cmd_score_report(args) == 0
    out = capsys.readouterr().out
    assert "0 sent · 0 skipped · 1 pending" in out


def test_cmd_score_report_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    notion = FakeNotion(tmp_path / "data" / "seen_domains.json")
    notion.add("p-A", "A")
    monkeypatch.setattr(runner, "get_sink", lambda *a: notion)
    args = argparse.Namespace(config="config.yaml", market=None, niche=None, min_band=None)
    assert runner.cmd_score_report(args) == 0
    assert "no drafted, sent or skipped pages yet (1 pages read)" in capsys.readouterr().out


def test_config_rejects_bad_score_report(tmp_path):
    from pipeline.config import ConfigError, load_config

    (tmp_path / "c.yaml").write_text(MARKETS_YAML + "score_report:\n  ab_ratio: 1\n")
    with pytest.raises(ConfigError, match="ab_ratio"):
        load_config(tmp_path / "c.yaml")
