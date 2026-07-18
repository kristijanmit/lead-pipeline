"""Notion sink — the §6.1 field mapping, degrade-don't-crash batch writes,
and the seen_domains.json cache that keeps sync idempotent without a live
Notion query per lead (ARCHITECTURE.md §9). No live network — a fake
session stands in for requests."""

import json

import pytest

from pipeline.config import DEFAULTS
from pipeline.schema import Lead
from pipeline.sinks.notion_sink import (
    NotionAuthError,
    NotionSink,
    cache_key,
    load_seen,
    save_seen,
    to_notion_properties,
)


def _lead(company="Zubar Bobić", domain="bobic.rs", status="scored", **overrides):
    overrides.setdefault("source", "osm")
    return Lead(company=company, domain=domain, status=status, **overrides)


class FakeResponse:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {"id": "page-x"}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Returns queued responses in order; records every POST payload."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def _sink(tmp_path, responses):
    config = {
        "notion": {
            **DEFAULTS["notion"],
            "database_id": "db-1",
            "delay_s": 0,
            "cache_path": str(tmp_path / "seen_domains.json"),
        }
    }
    session = FakeSession(responses)
    return NotionSink(config, token="secret-token", session=session), session


# --- to_notion_properties (ARCHITECTURE.md §6.1) ---


def test_multi_contact_split():
    props = to_notion_properties(
        _lead(
            contact_emails=["office@bobic.rs", "dr@bobic.rs", "info@bobic.rs"],
            contact_phones=["+381 21 452 333", "021/452-334"],
        )
    )
    assert props["Primary Email"] == {"email": "office@bobic.rs"}
    additional = props["Additional Emails"]["rich_text"][0]["text"]["content"]
    assert additional == "dr@bobic.rs, info@bobic.rs"
    assert props["Phone"] == {"phone_number": "+381 21 452 333"}
    phones = props["Additional Phones"]["rich_text"][0]["text"]["content"]
    assert phones == "021/452-334"


def test_no_website_lead_omits_domain():
    props = to_notion_properties(_lead(domain="", lead_type="new_build"))
    assert "Domain" not in props
    assert props["Lead Type"] == {"select": {"name": "new_build"}}
    assert props["Company"]["title"][0]["text"]["content"] == "Zubar Bobić"


def test_unknown_fields_are_omitted_not_blanked():
    props = to_notion_properties(_lead())  # nothing enriched or scored
    for absent in (
        "HTTPS",
        "Mobile Friendly",
        "CMS",
        "Performance Score",
        "Primary Email",
        "Phone",
        "Social Links",
        "Audit Notes",
        "Reachability Score",
        "Opportunity Score",
        "Total Score",
        "Lead Type",
    ):
        assert absent not in props
    # False is data, None is not
    assert to_notion_properties(_lead(https=False))["HTTPS"] == {"checkbox": False}
    # Qualified is a checkbox, always present — unlike the number/select
    # fields above, "unknown" and "False" aren't distinguishable for it
    assert props["Qualified"] == {"checkbox": False}


def test_source_and_select_mappings():
    assert to_notion_properties(_lead())["Source"] == {"select": {"name": "OSM/Maps"}}
    assert to_notion_properties(_lead(source="maps"))["Source"] == {
        "select": {"name": "OSM/Maps"}
    }
    assert to_notion_properties(_lead(source="manual"))["Source"] == {
        "select": {"name": "Manual"}
    }
    # Industry goes out raw — Notion auto-creates the select option
    assert to_notion_properties(_lead(industry="dentist"))["Industry"] == {
        "select": {"name": "dentist"}
    }


def test_location_maps_to_select_and_is_omitted_when_empty():
    assert to_notion_properties(_lead(location="Novi Sad"))["Location"] == {
        "select": {"name": "Novi Sad"}
    }
    assert "Location" not in to_notion_properties(_lead())


def test_social_links_render_as_markdown_lines():
    props = to_notion_properties(
        _lead(social_links={"linkedin": "https://l.example", "facebook": "https://f.example"})
    )
    text = props["Social Links"]["rich_text"][0]["text"]["content"]
    assert text == "[Facebook](https://f.example)\n[Linkedin](https://l.example)"


def test_scores_lighthouse_and_status():
    props = to_notion_properties(
        _lead(
            lighthouse={"performance": 42, "seo": 90},
            reachability_score=90.0,
            opportunity_score=70.0,
            total_score=63.5,
            qualified=True,
        )
    )
    assert props["Performance Score"] == {"number": 42}
    assert props["SEO Score"] == {"number": 90}
    assert "Accessibility Score" not in props  # key missing from the audit
    assert props["Reachability Score"] == {"number": 90.0}
    assert props["Opportunity Score"] == {"number": 70.0}
    assert props["Total Score"] == {"number": 63.5}
    assert props["Qualified"] == {"checkbox": True}
    assert props["Status"] == {"select": {"name": "Not started"}}


def test_bookkeeping_fields_never_synced():
    props = to_notion_properties(_lead(run_id="r1", errors=["audit: boom"]))
    dumped = json.dumps(props)
    assert "r1" not in dumped
    assert "boom" not in dumped
    assert "schema_version" not in dumped


# --- NotionSink.write ---


def test_write_creates_pages_and_cache_incrementally(tmp_path):
    sink, session = _sink(
        tmp_path,
        [FakeResponse(200, {"id": "page-1"}), FakeResponse(200, {"id": "page-2"})],
    )
    leads = [_lead("A", "a.rs"), _lead("B", "b.rs")]
    report = sink.write(leads)

    assert [lead.company for lead in report.created] == ["A", "B"]
    assert report.failed == [] and report.cached == []
    assert all(c["url"].endswith("/pages") for c in session.calls)
    assert session.calls[0]["json"]["parent"] == {"database_id": "db-1"}
    assert session.calls[0]["headers"]["Authorization"] == "Bearer secret-token"

    seen = load_seen(sink.cache_path)
    assert seen["domain:a.rs"]["page_id"] == "page-1"
    assert seen["domain:b.rs"]["page_id"] == "page-2"


def test_one_failure_does_not_halt_the_batch(tmp_path):
    # B gets a 500 twice (initial + the one retry) — batch continues to C
    sink, _ = _sink(
        tmp_path,
        [
            FakeResponse(200, {"id": "page-1"}),
            FakeResponse(500, {"message": "boom"}),
            FakeResponse(500, {"message": "boom"}),
            FakeResponse(200, {"id": "page-3"}),
        ],
    )
    leads = [_lead("A", "a.rs"), _lead("B", "b.rs"), _lead("C", "c.rs")]
    report = sink.write(leads)

    assert [lead.company for lead in report.created] == ["A", "C"]
    assert len(report.failed) == 1
    assert report.failed[0][0].company == "B" and "500" in report.failed[0][1]
    # the failed lead never entered the cache — a re-run retries it
    assert set(load_seen(sink.cache_path)) == {"domain:a.rs", "domain:c.rs"}


def test_retry_recovers_from_transient_429(tmp_path):
    sink, session = _sink(
        tmp_path,
        [
            FakeResponse(429, {"message": "slow down"}, headers={"Retry-After": "0"}),
            FakeResponse(200, {"id": "page-1"}),
        ],
    )
    report = sink.write([_lead("A", "a.rs")])
    assert len(report.created) == 1
    assert len(session.calls) == 2


def test_auth_failure_raises_immediately(tmp_path):
    sink, _ = _sink(tmp_path, [FakeResponse(401, {"message": "bad token"})])
    with pytest.raises(NotionAuthError, match="NOTION_TOKEN"):
        sink.write([_lead("A", "a.rs")])


def test_cached_leads_are_skipped_without_a_request(tmp_path):
    sink, session = _sink(tmp_path, [FakeResponse(200, {"id": "page-2"})])
    save_seen(sink.cache_path, {"domain:a.rs": {"page_id": "page-1", "synced_at": None}})
    report = sink.write([_lead("A", "a.rs"), _lead("B", "b.rs")])

    assert [lead.company for lead in report.cached] == ["A"]
    assert [lead.company for lead in report.created] == ["B"]
    assert len(session.calls) == 1


def test_limit_caps_attempts_not_successes(tmp_path):
    sink, _ = _sink(tmp_path, [FakeResponse(200, {"id": "page-1"})])
    save_seen(sink.cache_path, {"domain:a.rs": {"page_id": "page-1", "synced_at": None}})
    report = sink.write(
        [_lead("A", "a.rs"), _lead("B", "b.rs"), _lead("C", "c.rs")], limit=1
    )
    # the cached lead doesn't consume the limit; C is left pending
    assert [lead.company for lead in report.cached] == ["A"]
    assert [lead.company for lead in report.created] == ["B"]
    assert report.pending == 1

    # a failed attempt still consumes the limit — a dry run against a broken
    # setup must not burn through the whole batch failing lead by lead
    sink, session = _sink(
        tmp_path,
        [FakeResponse(400, {"message": "bad"}), FakeResponse(400, {"message": "bad"})],
    )
    report = sink.write([_lead("D", "d.rs"), _lead("E", "e.rs")], limit=1)
    assert len(report.failed) == 1 and report.pending == 1
    assert len(session.calls) == 1


def test_missing_token_is_a_clear_startup_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    config = {"notion": {**DEFAULTS["notion"], "database_id": "db-1"}}
    with pytest.raises(ValueError, match="NOTION_TOKEN"):
        NotionSink(config)


# --- cache helpers ---


def test_cache_key_string_form():
    assert cache_key(_lead(domain="a.rs")) == "domain:a.rs"
    assert cache_key(_lead(company="No Site Doo", domain="")) == "company:no site doo"


def test_seen_round_trip(tmp_path):
    path = tmp_path / "nested" / "seen_domains.json"
    entries = {"domain:a.rs": {"page_id": "p1", "synced_at": "2026-07-16T00:00:00+00:00"}}
    save_seen(path, entries)
    assert load_seen(path) == entries
    assert load_seen(tmp_path / "missing.json") == {}


# --- email-based duplicate matching (the Mirković bug) ---


def test_same_email_different_domain_matches_existing_cache_entry(tmp_path):
    # "Mirković" (no domain) synced first, keyed by company; "Dental Centar
    # Mirković" (domain resolved later) shares its email — must not become
    # a second page
    sink, session = _sink(tmp_path, [])
    save_seen(
        sink.cache_path,
        {
            "company:mirković": {
                "page_id": "page-1",
                "synced_at": None,
                "snapshot": {
                    "emails": ["info@dentalcentarmirkovic.com"],
                    "phones": [],
                    "socials": {},
                    "domain": "",
                    "company": "Mirković",
                },
            }
        },
    )
    lead = _lead(
        company="Dental Centar Mirković",
        domain="dentalcentarmirkovic.com",
        contact_emails=["info@dentalcentarmirkovic.com"],
    )
    report = sink.write([lead])

    assert report.created == []
    assert [l.company for l in report.cached] == ["Dental Centar Mirković"]
    assert session.calls == []  # no page-create request at all


def test_new_info_on_a_duplicate_posted_as_comment(tmp_path):
    sink, session = _sink(tmp_path, [FakeResponse(200, {"id": "comment-1"})])
    save_seen(
        sink.cache_path,
        {
            "company:mirković": {
                "page_id": "page-1",
                "synced_at": None,
                "snapshot": {
                    "emails": ["info@dentalcentarmirkovic.com"],
                    "phones": [],
                    "socials": {},
                    "domain": "",
                    "company": "Mirković",
                },
            }
        },
    )
    lead = _lead(
        company="Dental Centar Mirković",
        domain="dentalcentarmirkovic.com",
        contact_emails=["info@dentalcentarmirkovic.com"],
        contact_phones=["+381 69 54 14 515"],
        social_links={"facebook": "https://facebook.com/dentalcentarmirkovic"},
    )
    report = sink.write([lead])

    assert report.cached and report.failed == []
    assert len(session.calls) == 1
    assert session.calls[0]["url"].endswith("/comments")
    assert session.calls[0]["json"]["parent"] == {"page_id": "page-1"}
    comment_text = session.calls[0]["json"]["rich_text"][0]["text"]["content"]
    assert "+381 69 54 14 515" in comment_text
    assert "facebook" in comment_text

    # the snapshot is updated so a third sync with the same info doesn't
    # post the same comment again
    updated = load_seen(sink.cache_path)["company:mirković"]["snapshot"]
    assert "+381 69 54 14 515" in updated["phones"]


def test_duplicate_with_no_new_info_posts_no_comment(tmp_path):
    sink, session = _sink(tmp_path, [])
    save_seen(
        sink.cache_path,
        {
            "domain:a.rs": {
                "page_id": "page-1",
                "synced_at": None,
                "snapshot": {
                    "emails": ["office@a.rs"],
                    "phones": [],
                    "socials": {},
                    "domain": "a.rs",
                    "company": "A",
                },
            }
        },
    )
    report = sink.write([_lead("A", "a.rs", contact_emails=["office@a.rs"])])
    assert report.cached and session.calls == []


def test_refresh_cache_paginates_and_normalizes(tmp_path):
    def page(page_id, domain, company):
        return {
            "id": page_id,
            "properties": {
                "Domain": {"url": domain},
                "Company": {"title": [{"plain_text": company}]},
            },
        }

    sink, session = _sink(
        tmp_path,
        [
            FakeResponse(
                200,
                {
                    "results": [page("p1", "https://www.A.rs/about", "A")],
                    "has_more": True,
                    "next_cursor": "cur-2",
                },
            ),
            FakeResponse(
                200,
                {"results": [page("p2", None, "No Site Doo")], "has_more": False},
            ),
        ],
    )
    count = sink.refresh_cache()

    assert count == 2
    assert session.calls[0]["url"].endswith("/databases/db-1/query")
    assert session.calls[1]["json"]["start_cursor"] == "cur-2"
    seen = load_seen(sink.cache_path)
    assert seen["domain:a.rs"]["page_id"] == "p1"  # normalized via dedupe rules
    assert seen["company:no site doo"]["page_id"] == "p2"


def test_refresh_cache_rebuilds_snapshots_for_email_matching(tmp_path):
    sink, _ = _sink(
        tmp_path,
        [
            FakeResponse(
                200,
                {
                    "results": [
                        {
                            "id": "p1",
                            "properties": {
                                "Domain": {"url": "dentalcentarmirkovic.com"},
                                "Company": {"title": [{"plain_text": "Dental Centar Mirković"}]},
                                "Primary Email": {"email": "info@dentalcentarmirkovic.com"},
                                "Additional Emails": {
                                    "rich_text": [
                                        {"plain_text": "dentalcentar.mirkovic@gmail.com"}
                                    ]
                                },
                                "Phone": {"phone_number": "021 3822824"},
                                "Social Links": {
                                    "rich_text": [
                                        {"plain_text": "[Facebook](https://facebook.com/mirkovic)"}
                                    ]
                                },
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        ],
    )
    sink.refresh_cache()
    snapshot = load_seen(sink.cache_path)["domain:dentalcentarmirkovic.com"]["snapshot"]
    assert snapshot["emails"] == [
        "info@dentalcentarmirkovic.com",
        "dentalcentar.mirkovic@gmail.com",
    ]
    assert snapshot["phones"] == ["021 3822824"]
    assert snapshot["socials"] == {"facebook": "https://facebook.com/mirkovic"}
