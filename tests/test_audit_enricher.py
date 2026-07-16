import json
from pathlib import Path

import pytest

from pipeline.enrichers.audit import (
    build_audit_notes,
    detect_cms,
    parse_lighthouse_report,
)
from pipeline.schema import Lead

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def report() -> dict:
    return json.loads((FIXTURES / "lighthouse_sample.json").read_text(encoding="utf-8"))


def test_parse_lighthouse_report(report):
    scores, mobile_friendly = parse_lighthouse_report(report)
    assert scores == {
        "performance": 42,
        "accessibility": 88,
        "best_practices": 75,
        "seo": 91,
    }
    assert mobile_friendly is True


def test_parse_report_tolerates_null_scores(report):
    report["categories"]["performance"]["score"] = None  # category errored out
    del report["audits"]["viewport"]
    scores, mobile_friendly = parse_lighthouse_report(report)
    assert "performance" not in scores
    assert scores["seo"] == 91
    assert mobile_friendly is None


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<link href="/wp-content/themes/x/style.css">', "WordPress"),
        ('<meta name="generator" content="WordPress 6.4">', "WordPress"),
        ('<meta name="GENERATOR" content="Joomla! - Open Source">', "Joomla"),
        ('<script src="https://cdn.shopify.com/s/x.js"></script>', "Shopify"),
        ('<img src="https://static.wixstatic.com/media/x.png">', "Wix"),
        ('<script src="https://assets.website-files.com/x.js"></script>', "Webflow"),
        ('<meta name="generator" content="Hugo 0.121.0">', "Hugo 0.121.0"),
        ("<p>hand-written html</p>", None),
    ],
)
def test_detect_cms(html, expected):
    assert detect_cms(html) == expected


def test_audit_notes_full_lead():
    lead = Lead(
        company="A",
        domain="a.rs",
        source="osm",
        contact_emails=["info@a.rs", "sales@a.rs"],
        contact_phones=["+381 21 1", "+381 21 2"],
        social_links={"facebook": "https://facebook.com/a", "x": "https://x.com/a"},
        https=True,
        mobile_friendly=False,
        cms="WordPress",
        lighthouse={"performance": 42, "accessibility": 88, "best_practices": 75, "seo": 91},
    )
    assert build_audit_notes(lead) == (
        "2 emails, 2 phones, 2 socials; LH perf 42 / a11y 88 / bp 75 / seo 91; "
        "https ok; not mobile friendly; cms: WordPress"
    )


def test_audit_notes_no_lighthouse_data():
    lead = Lead(company="A", domain="a.rs", source="osm", https=False)
    assert build_audit_notes(lead) == (
        "0 emails, 0 phones, 0 socials; no Lighthouse data; no https"
    )


def test_audit_notes_no_website():
    lead = Lead(company="A", domain="", source="manual", contact_phones=["060 1"])
    assert build_audit_notes(lead) == "0 emails, 1 phones, 0 socials; no website"
