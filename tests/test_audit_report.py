"""audit-report (v2 step 8): findings ranked like the scorer's Need items, skip rules, sr/en
wording, Markdown -> HTML, and the CLI (files, kept hand edits, --rescan, stale warning,
manifest). Hand-built leads, no network: --rescan uses a faked audit core."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from pipeline.audit import AuditResult
from pipeline.config import DEFAULTS
from pipeline.enrichers.audit import AuditEnricher
from pipeline.reports import MANUAL_PLACEHOLDER, build_report, findings, markdown_to_html, skip_reason
from pipeline.reports.audit_report import positives
from pipeline.runner import main
from pipeline.schema import Lead, read_jsonl, write_jsonl
from support import MARKETS_YAML, SERBIA_BLOCK

NEED = DEFAULTS["scoring"]["need"]
YEAR = 2026
DAY = date(2026, 11, 27)
SIG = "Kristijan, ONIX Web Lab, onixweblab.com"


def _lead(**kw):
    base = dict(
        company="Smile Dental", domain="smile.rs", source="maps", language="sr", market="serbia",
        site_reachable=True, https=True, mobile_friendly=True, has_viewport_meta=True,
        lighthouse={"performance": 80, "accessibility": 90, "best_practices": 90, "seo": 90},
        copyright_year=YEAR, has_booking=True, has_click_to_call=True, has_messaging_link=False,
    )
    return Lead(**{**base, **kw})


def _items(lead, **kw):
    return [f.item for f in findings(lead, NEED, YEAR, **kw)]


# --- findings ----------------------------------------------------------------------

def test_findings_ranked_by_need_points_and_capped_at_three():
    lead = _lead(
        https=False, mobile_friendly=False, copyright_year=2018, has_booking=False, has_click_to_call=False,
        lighthouse={"performance": 30, "seo": 50},
    )
    assert _items(lead) == ["not_mobile", "no_https", "outdated"]


def test_niche_need_overrides_reorder_and_drop_items():
    lead = _lead(https=False, mobile_friendly=False, has_booking=False, has_click_to_call=False)
    assert _items(lead, need_overrides={"not_mobile": 0, "no_booking_or_call": 40}) == [
        "no_booking_or_call", "no_https",
    ]


def test_lighthouse_items_left_out_when_lighthouse_failed():
    lead = _lead(lighthouse=None, mobile_friendly=None, has_viewport_meta=False, https=False)
    assert _items(lead) == ["not_mobile", "no_https"]  # mobile read from the viewport meta


def test_outdated_names_the_year_or_the_legacy_builder():
    by_year = findings(_lead(copyright_year=2019), NEED, YEAR)[0]
    assert (by_year.text_key, by_year.params) == ("outdated", {"year": 2019})
    by_cms = findings(_lead(cms="Joomla"), NEED, YEAR)[0]
    assert (by_cms.text_key, by_cms.params) == ("outdated_cms", {"cms": "Joomla"})


def test_certificate_error_is_a_finding():
    lead = _lead(site_reachable=False, probe_error="ssl", https=None, lighthouse=None)
    assert skip_reason(lead) is None
    assert _items(lead) == ["cert_error"]
    assert positives(lead, YEAR) == []


def test_a_fine_site_has_no_findings():
    assert _items(_lead()) == []


@pytest.mark.parametrize("kw, reason", [
    (dict(domain=""), "no website"),
    (dict(domain="instagram.com"), "no website"),
    (dict(site_reachable=False, probe_error="dns"), "domain dead"),
    (dict(site_reachable=False, probe_error="refused"), "domain dead"),
    (dict(site_reachable=False, probe_error="timeout"), "site didn't load"),
    (dict(site_reachable=None, lighthouse=None), "not audited"),
    (dict(language="de"), "no report wording"),
])
def test_skip_reasons(kw, reason):
    assert skip_reason(_lead(**kw)).startswith(reason)


def test_positives_only_from_known_true_signals():
    lead = _lead(lighthouse={"performance": 95}, has_messaging_link=True)
    assert positives(lead, YEAR) == ["https", "mobile", "fast", "click_to_call", "booking", "messaging", "recent"]
    assert positives(_lead(https=None, mobile_friendly=None, has_viewport_meta=None, has_booking=None,
                           has_click_to_call=None, copyright_year=None), YEAR) == []


# --- wording -----------------------------------------------------------------------

_BANNED = ("lighthouse", "seo", "rating", "review", "★", "free", "besplatn", "score")


@pytest.mark.parametrize("language", ["sr", "en"])
def test_report_wording_has_no_banned_terms(language):
    from pipeline.reports.audit_report import TEXTS
    t = TEXTS[language]
    text = " ".join([*(" ".join(v) for v in t.FINDINGS.values()), *t.WORKS.values(), t.CLOSING, t.INTRO]).lower()
    assert not [w for w in _BANNED if w in text]
    assert set(t.FINDINGS) == {"cert_error", "not_mobile", "no_https", "outdated", "outdated_cms",
                               "perf_below_50", "no_booking_or_call", "seo_below_70"}


def test_serbian_report():
    lead = _lead(https=False, mobile_friendly=False)
    md = build_report(lead, findings(lead, NEED, YEAR), positives(lead, YEAR), DAY, SIG)
    assert md.startswith("# Provera sajta: Smile Dental\n")
    assert "smile.rs · provereno 27. 11. 2026." in md
    assert "1. **Teško se koristi na telefonu**" in md and "2. **Pregledač ga označava" in md
    assert "## Šta već dobro radi" in md and "- Online zakazivanje" in md
    assert MANUAL_PLACEHOLDER in md and md.rstrip().endswith(SIG)


def test_english_report():
    lead = _lead(language="en", domain="smile.ae", lighthouse={"performance": 20}, has_booking=False,
                 has_click_to_call=False)
    md = build_report(lead, findings(lead, NEED, YEAR), positives(lead, YEAR), DAY, SIG)
    assert "# Website check: Smile Dental" in md and "smile.ae · checked 27 November 2026" in md
    assert "1. **Slow to load on a phone**" in md and "2. **No quick way to get in touch**" in md
    assert "## What already works" in md


def test_build_report_refuses_an_empty_report():
    with pytest.raises(ValueError):
        build_report(_lead(), [], [], DAY, SIG)


# --- Markdown -> HTML ---------------------------------------------------------------

def test_html_is_self_contained_and_drops_the_empty_placeholder():
    lead = _lead(https=False, mobile_friendly=False)
    md = build_report(lead, findings(lead, NEED, YEAR), positives(lead, YEAR), DAY, SIG)
    out = markdown_to_html(md, "sr")
    assert '<html lang="sr">' in out and "<style>" in out and "ONIX Web Lab" in out
    assert "<link" not in out and "<script" not in out and "http" not in out.split("<body>")[1].replace("https)", "")
    assert "add your own finding" not in out
    assert out.count("<ol>") == 1 and out.count("<li>") == 2 + len(positives(lead, YEAR))
    assert "<strong>" not in out.split("<ol>")[0]  # bold titles become title spans inside the list


def test_a_hand_written_finding_joins_the_list():
    md = "# T\n\n1. **One**\n   a\n\n2. **Hand** <b>x</b>\n   added by hand\n\n<!-- add your own finding -->\n"
    out = markdown_to_html(md, "en")
    assert out.count("<ol>") == 1 and "<strong>Hand</strong>" in out and "&lt;b&gt;" in out


# --- CLI ----------------------------------------------------------------------------

def _make_run(tmp_path, monkeypatch, leads, enriched_at=None, market=SERBIA_BLOCK):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(MARKETS_YAML)
    run_dir = tmp_path / "data" / "runs" / "r1"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "scored.jsonl", leads)
    stamp = (enriched_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    manifest = {"run_id": "r1", "enrich": {"finished_at": stamp}}
    if market:
        manifest["market"] = market
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    return run_dir


def _scored(**kw):
    kw.setdefault("qualified", True)
    kw.setdefault("tier", "B")
    kw.setdefault("priority", 50.0)
    kw.setdefault("status", "scored")
    return _lead(**kw)


def test_cli_writes_reports_skips_and_records_manifest(tmp_path, monkeypatch, capsys):
    run_dir = _make_run(tmp_path, monkeypatch, [
        _scored(company="Bad", domain="bad.rs", https=False),
        _scored(company="Fine", domain="fine.rs"),
        _scored(company="NoSite", domain="", lead_type="new_build"),
        _scored(company="Low", domain="low.rs", https=False, tier="D"),
        _scored(company="Out", domain="out.rs", https=False, qualified=False, tier=None),
    ])
    assert main(["audit-report", "--run", "r1"]) == 0
    reports = run_dir / "reports"
    assert sorted(p.name for p in reports.iterdir()) == ["bad.rs.html", "bad.rs.md"]
    assert "Provera sajta: Bad" in (reports / "bad.rs.md").read_text()
    block = json.loads((run_dir / "manifest.json").read_text())["audit_report"]
    assert block["counts"]["written"] == 1
    assert block["counts"]["skipped"] == {"site looks fine": 1, "no website": 1}
    assert block["counts"]["selected"] == 3  # D and unqualified left out at --min-tier C
    assert read_jsonl(run_dir / "scored.jsonl")[0].https is False  # JSONL untouched


def test_cli_keeps_a_hand_edited_report_and_rerenders_its_html(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, monkeypatch, [_scored(domain="bad.rs", https=False)])
    main(["audit-report", "--run", "r1"])
    md_path = run_dir / "reports" / "bad.rs.md"
    md_path.write_text(md_path.read_text().replace(MANUAL_PLACEHOLDER, "2. **Hand finding**\n   by hand"))
    main(["audit-report", "--run", "r1"])
    assert "Hand finding" in md_path.read_text()
    assert "Hand finding" in (run_dir / "reports" / "bad.rs.html").read_text()
    main(["audit-report", "--run", "r1", "--force"])
    assert "Hand finding" not in md_path.read_text()


def test_cli_language_falls_back_to_the_run_market(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, monkeypatch, [_scored(domain="bad.rs", https=False, language="")])
    main(["audit-report", "--run", "r1"])
    assert "Provera sajta" in (run_dir / "reports" / "bad.rs.md").read_text()


def test_cli_warns_when_the_audit_is_older_than_14_days(tmp_path, monkeypatch, capsys):
    old = datetime.now(timezone.utc) - timedelta(days=20)
    run_dir = _make_run(tmp_path, monkeypatch, [_scored(domain="bad.rs", https=False)], enriched_at=old)
    main(["audit-report", "--run", "r1"])
    assert "20 days old" in capsys.readouterr().err
    assert f"provereno {old.day}. {old.month}. {old.year}." in (run_dir / "reports" / "bad.rs.md").read_text()


def test_cli_domain_selects_one_lead_and_reports_why_it_was_skipped(tmp_path, monkeypatch, capsys):
    _make_run(tmp_path, monkeypatch, [_scored(domain="fine.rs", tier="D", qualified=False)])
    assert main(["audit-report", "--run", "r1", "--domain", "https://www.fine.rs/"]) == 0
    assert "skipped, site looks fine" in capsys.readouterr().out
    assert main(["audit-report", "--run", "r1", "--domain", "nope.rs"]) == 1


def test_cli_rescan_uses_the_audit_core_and_todays_date(tmp_path, monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    run_dir = _make_run(tmp_path, monkeypatch, [_scored(domain="fixed.rs", https=False)], enriched_at=old)
    calls = []

    def fake_audit(self, domain):
        calls.append(domain)
        return AuditResult(site_reachable=True, url=f"https://{domain}/", https=True,
                           signals={"has_viewport_meta": False, "has_booking": True, "has_click_to_call": True},
                           lighthouse={"performance": 80, "seo": 90}, mobile_friendly=False)

    monkeypatch.setattr(AuditEnricher, "audit", fake_audit)
    assert main(["audit-report", "--run", "r1", "--rescan"]) == 0
    md = (run_dir / "reports" / "fixed.rs.md").read_text()
    assert calls == ["fixed.rs"]
    assert "Teško se koristi na telefonu" in md and "nije bezbedno" not in md.split("##")[0].split("popraviti")[1]
    today = datetime.now().date()
    assert f"provereno {today.day}. {today.month}. {today.year}." in md
    assert read_jsonl(run_dir / "scored.jsonl")[0].https is False  # rescan is for the report only


def test_cli_limit_and_priority_order(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, monkeypatch, [
        _scored(company="Lo", domain="lo.rs", https=False, priority=40.0),
        _scored(company="Hi", domain="hi.rs", https=False, priority=70.0, tier="A"),
    ])
    main(["audit-report", "--run", "r1", "--limit", "1"])
    assert [p.name for p in (run_dir / "reports").glob("*.md")] == ["hi.rs.md"]


def test_cli_language_from_the_scored_market_for_runs_before_markets(tmp_path, monkeypatch):
    run_dir = _make_run(tmp_path, monkeypatch, [_scored(domain="bad.rs", https=False, language="", market="")],
                        market=None)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["score"] = {"config_snapshot": {"market": "serbia"}}
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    main(["audit-report", "--run", "r1"])
    assert "Provera sajta" in (run_dir / "reports" / "bad.rs.md").read_text()
