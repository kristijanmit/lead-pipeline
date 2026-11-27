"""Notion sink — the §6.1 field mapping, degrade-don't-crash batch writes,
and the seen_domains.json cache that keeps sync idempotent without a live
Notion query per lead (docs/ARCHITECTURE.md §9). No live network — a fake
session stands in for requests."""

import json
from datetime import date

import pytest

from pipeline.config import DEFAULTS
from pipeline.schema import Lead
from pipeline.sinks.notion_sink import (
    NotionAuthError,
    NotionSink,
    blocking_seen,
    cache_key,
    is_legacy,
    legacy_block_reason,
    legacy_info,
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


# --- to_notion_properties (docs/ARCHITECTURE.md §6.1) ---


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
        "Lighthouse",
        "Address",
        "Rating",
        "Review Count",
        "Profile URL",
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


def test_social_links_render_as_clickable_rich_text():
    props = to_notion_properties(
        _lead(social_links={"linkedin": "https://l.example", "facebook": "https://f.example"})
    )
    items = props["Social Links"]["rich_text"]
    assert [i["text"]["content"] for i in items] == ["Facebook", "\n", "Linkedin"]
    assert items[0]["text"]["link"] == {"url": "https://f.example"}
    assert items[2]["text"]["link"] == {"url": "https://l.example"}


def test_outreach_fields_and_queued_status():
    queued = to_notion_properties(
        _lead(qualified=True, suggested_channel="Instagram", draft_message="Dobar dan")
    )
    assert queued["Status"] == {"select": {"name": "Queued"}}
    assert queued["Suggested Channel"] == {"select": {"name": "Instagram"}}
    assert queued["Draft Message"]["rich_text"][0]["text"]["content"] == "Dobar dan"
    plain = to_notion_properties(_lead(qualified=True))
    assert plain["Status"] == {"select": {"name": "Not started"}}
    assert "Draft Message" not in plain and "Suggested Channel" not in plain


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
    # one text property, fixed order, keys missing from the audit are skipped
    assert props["Lighthouse"]["rich_text"][0]["text"]["content"] == "Performance: 42\nSEO: 90"
    assert "Performance Score" not in props
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
                                        {"plain_text": "Facebook", "href": "https://facebook.com/mirkovic"},
                                        {"plain_text": "\n[Instagram](https://instagram.com/mirkovic)"},
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
    assert snapshot["socials"] == {
        "facebook": "https://facebook.com/mirkovic",
        "instagram": "https://instagram.com/mirkovic",  # older Markdown-text pages still parse
    }


def test_listing_details():
    props = to_notion_properties(
        _lead(address="Železnička 5, Novi Sad", rating=4.5, review_count=34, profile_url="https://maps.google.com/?cid=1")
    )
    assert props["Address"]["rich_text"][0]["text"]["content"] == "Železnička 5, Novi Sad"
    assert props["Rating"] == {"number": 4.5}
    assert props["Review Count"] == {"number": 34}
    assert props["Profile URL"] == {"url": "https://maps.google.com/?cid=1"}


def _manual_page():
    def rt(s):
        return {"rich_text": [{"plain_text": s}]}
    return {"id": "p1", "properties": {
        "Company": {"title": [{"plain_text": "Salon Mira"}]},
        "Industry": {"select": {"name": "hairdresser"}},
        "Location": {"select": {"name": "Novi Sad"}},
        "Social Links": {"rich_text": [{"plain_text": "Instagram", "href": "https://instagram.com/mira"}]},
        "SM Audit": rt("posts daily, no booking link"),
        "Status": {"select": {"name": "Not started"}},
    }}


def test_lead_from_manual_page_and_sm_audit_roundtrip():
    from pipeline.sinks.notion_sink import lead_from_page, to_notion_properties

    lead = lead_from_page(_manual_page(), country="Serbia")
    assert (lead.company, lead.domain, lead.industry, lead.location) == ("Salon Mira", "", "hairdresser", "Novi Sad")
    assert lead.social_links == {"instagram": "https://instagram.com/mira"}
    assert lead.sm_audit == "posts daily, no booking link" and lead.lead_type == "new_build" and lead.qualified
    props = to_notion_properties(lead)
    assert props["SM Audit"]["rich_text"][0]["text"]["content"] == "posts daily, no booking link"


@pytest.mark.parametrize(
    "location, expected",
    [
        ("Sabac, Serbia", "Sabac"),
        ("Šabac", "Šabac"),
        ("  Novi Sad , Serbia ", "Novi Sad"),
    ],
)
def test_location_select_has_city_only(location, expected):
    props = to_notion_properties(_lead(location=location))
    assert props["Location"] == {"select": {"name": expected}}


def test_select_values_never_contain_commas():
    props = to_notion_properties(_lead(industry="auto, elektrika"))
    assert "," not in props["Industry"]["select"]["name"]


def test_empty_location_is_omitted():
    assert "Location" not in to_notion_properties(_lead(location=", Serbia"))


def test_lead_from_synced_page_reads_audit_fields():
    from pipeline.sinks.notion_sink import lead_from_page

    page = _manual_page()
    page["properties"].update({
        "Source": {"select": {"name": "OSM/Maps"}},
        "Audit Notes": {"rich_text": [{"plain_text": "no HTTPS"}]},
        "CMS": {"select": {"name": "wordpress"}},
        "Lead Type": {"select": {"name": "redesign"}},
    })
    lead = lead_from_page(page)
    assert (lead.source, lead.audit_notes, lead.cms, lead.lead_type) == ("osm", "no HTTPS", "wordpress", "redesign")


def _draftable_sink(monkeypatch):
    from pipeline.sinks.notion_sink import NotionSink
    sink = NotionSink.__new__(NotionSink)
    sink.database_id = "db"
    sent = []
    monkeypatch.setattr(sink, "_post", lambda url, payload: sent.append(payload) or {"results": []}, raising=False)
    return sink, sent


def _conditions(payload):
    return payload["filter"]["and"]


def _or_values(condition):
    return [(c["property"], c["select"]) for c in condition["or"]]


def test_query_draftable_filter_follows_auto_draft_eligibility(monkeypatch):
    sink, sent = _draftable_sink(monkeypatch)
    sink.query_draftable()
    status, qualified, verdict, tier, campaign, draft = _conditions(sent[-1])
    assert _or_values(status) == [("Status", {"equals": "Not started"}), ("Status", {"is_empty": True})]
    assert qualified == {"property": "Qualified", "checkbox": {"equals": True}}
    # an empty Verdict must pass: Skip is the only value that stops a page
    assert _or_values(verdict) == [("Verdict", {"is_empty": True}), ("Verdict", {"does_not_equal": "Skip"})]
    # empty Tier (hand-made / backfilled page) stays eligible; default minimum B
    assert _or_values(tier) == [
        ("Tier", {"is_empty": True}), ("Tier", {"equals": "A"}), ("Tier", {"equals": "B"}),
    ]
    # legacy-v1 is never drafted; an empty Campaign (hand-made page) still is
    assert _or_values(campaign) == [
        ("Campaign", {"is_empty": True}), ("Campaign", {"does_not_equal": "legacy-v1"}),
    ]
    assert draft == {"property": "Draft Message", "rich_text": {"is_empty": True}}
    assert "SM Audit" not in json.dumps(sent[-1])  # the note is optional now


def test_query_draftable_options(monkeypatch):
    sink, sent = _draftable_sink(monkeypatch)
    sink.query_draftable(min_tier="C")
    assert [v for _, v in _or_values(_conditions(sent[-1])[3])][-1] == {"equals": "C"}
    sink.query_draftable(redraft=True)
    conditions = _conditions(sent[-1])
    assert "Draft Message" not in json.dumps(conditions)
    assert ("Status", {"equals": "Queued"}) in _or_values(conditions[0])
    assert ("Status", {"equals": "Draft failed"}) in _or_values(conditions[0])
    sink.query_draftable(company="Mira")
    assert _conditions(sent[-1])[-1] == {"property": "Company", "title": {"contains": "Mira"}}


def test_query_draftable_scopes_to_page_ids(monkeypatch):
    sink, sent = _draftable_sink(monkeypatch)
    monkeypatch.setattr(sink, "_post", lambda url, payload: {"results": [{"id": "a"}, {"id": "b"}]})
    assert [p["id"] for p in sink.query_draftable(page_ids={"b"})] == ["b"]


# --- v2 step 6: market / niche / score properties, --rescore ---


def _v4_lead(**overrides):
    base = dict(
        market="serbia", language="sr", industry="dental clinic", niche="dental clinic",
        niche_label="stomatološke ordinacije", qualified=True, lead_type="redesign",
        need_score=70.0, viability_score=55.5, reach_score=100, priority=64.2, tier="B",
        score_reasons=["niche match", "no HTTPS", "best channel: Instagram"],
        score_confidence="high", audit_notes="WordPress site, no HTTPS.",
    )
    base.update(overrides)
    return _lead(**base)


def test_to_notion_properties_writes_market_niche_and_scores():
    props = to_notion_properties(_v4_lead())
    assert props["Market"] == {"select": {"name": "serbia"}}
    assert props["Language"] == {"select": {"name": "sr"}}
    assert props["Industry"] == {"select": {"name": "dental clinic"}}
    assert props["Niche Label"]["rich_text"][0]["text"]["content"] == "stomatološke ordinacije"
    assert props["Campaign"] == {"select": {"name": "dental clinic"}}  # the run's niche name
    assert props["Tier"] == {"select": {"name": "B"}}
    assert props["Priority"] == {"number": 64.2}
    assert props["Score Reasons"]["rich_text"][0]["text"]["content"] == (
        "Need 70 · Viability 55.5 · Reach 100 · niche match · no HTTPS · best channel: Instagram"
    )
    for absent in ("Market", "Language", "Niche Label", "Campaign", "Tier", "Priority", "Score Reasons", "Runs Ads"):
        assert absent not in to_notion_properties(_lead())


def test_disqualified_lead_score_reasons_have_no_subscores():
    lead = _v4_lead(qualified=False, need_score=None, viability_score=None, reach_score=None,
                    priority=None, tier=None, score_reasons=["disqualified: chain: 4 listings"])
    props = to_notion_properties(lead)
    assert props["Score Reasons"]["rich_text"][0]["text"]["content"] == "disqualified: chain: 4 listings"
    assert "Tier" not in props and "Priority" not in props


@pytest.mark.parametrize("ads_tags, expected", [(["meta_pixel"], True), ([], False)])
def test_runs_ads_prefilled_from_ads_tags_at_creation(ads_tags, expected):
    assert to_notion_properties(_lead(ads_tags=ads_tags))["Runs Ads"] == {"checkbox": expected}


def test_score_properties_touch_only_score_fields():
    from pipeline.sinks.notion_sink import SCORE_PROPERTIES, score_properties

    props = score_properties(_v4_lead(ads_tags=["gtm"], sm_audit="note", draft_message="hi",
                                      suggested_channel="Instagram"))
    assert tuple(props) == SCORE_PROPERTIES
    for human in ("Verdict", "Skip Reason", "SM Audit", "Status", "Communication", "Draft Message",
                  "Draft Notes", "Suggested Channel", "Chosen Channel", "Runs Ads", "Outcome",
                  "Booking Method", "Discovery Notes", "Price Challenged", "Market", "Language"):
        assert human not in props
    assert props["Audit Notes"]["rich_text"][0]["text"]["content"] == "WordPress site, no HTTPS."


def test_score_properties_clear_tier_of_a_newly_disqualified_lead():
    from pipeline.sinks.notion_sink import score_properties

    props = score_properties(_v4_lead(qualified=False, tier=None, priority=None, need_score=None,
                                      viability_score=None, reach_score=None, lead_type=None,
                                      score_reasons=[], audit_notes=""))
    assert props["Tier"] == {"select": None} and props["Priority"] == {"number": None}
    assert props["Qualified"] == {"checkbox": False} and props["Lead Type"] == {"select": None}
    assert props["Score Reasons"] == {"rich_text": []} and props["Audit Notes"] == {"rich_text": []}


def test_lead_from_page_reads_market_niche_and_scores():
    from pipeline.sinks.notion_sink import lead_from_page

    page = {"id": "p1", "properties": to_notion_properties(_v4_lead())}
    # Notion returns rich text with plain_text; mirror that
    for name in ("Niche Label", "Score Reasons", "Audit Notes"):
        page["properties"][name] = {"rich_text": [
            {"plain_text": t["text"]["content"]} for t in page["properties"][name]["rich_text"]
        ]}
    page["properties"]["Market"] = {"select": {"name": "Serbia"}}  # option name casing doesn't matter
    lead = lead_from_page(page)
    assert (lead.market, lead.language, lead.industry, lead.niche) == ("serbia", "sr", "dental clinic", "dental clinic")
    assert lead.niche_label == "stomatološke ordinacije"
    assert (lead.tier, lead.priority) == ("B", 64.2)
    assert lead.score_reasons == ["niche match", "no HTTPS", "best channel: Instagram"]


def test_lead_from_page_without_new_properties_is_blank():
    from pipeline.sinks.notion_sink import lead_from_page

    lead = lead_from_page(_manual_page())
    assert (lead.market, lead.language, lead.niche_label, lead.tier, lead.priority) == ("", "", "", None, None)
    assert lead.niche == "hairdresser" and lead.score_reasons == []


class PatchSession(FakeSession):
    def __init__(self, patch_codes=()):
        super().__init__([])
        self.patch_codes = list(patch_codes)
        self.patches = []

    def patch(self, url, headers=None, json=None, timeout=None):
        self.patches.append({"url": url, "json": json})
        return FakeResponse(self.patch_codes.pop(0) if self.patch_codes else 200)


def test_rescore_updates_cached_pages_and_reports_the_rest(tmp_path):
    sink, _ = _sink(tmp_path, [])
    session = PatchSession(patch_codes=[200, 500])
    sink.session = session
    save_seen(sink.cache_path, {
        "domain:bobic.rs": {"page_id": "page-1", "snapshot": {"emails": []}},
        "domain:old.rs": {"page_id": "page-2", "snapshot": {"emails": ["office@mira.rs"]}},
        "domain:fail.rs": {"page_id": "page-3", "snapshot": {"emails": []}},
    })
    report = sink.rescore([
        _v4_lead(),
        _v4_lead(company="Mira", domain="mira.rs", contact_emails=["office@mira.rs"]),  # email match
        _v4_lead(company="New", domain="new.rs"),  # never synced
    ])
    assert [l.company for l in report.updated] == ["Zubar Bobić"]
    assert [l.company for l, _ in report.failed] == ["Mira"]
    assert [l.company for l in report.no_page] == ["New"]
    assert [c["url"].rsplit("/", 1)[-1] for c in session.patches] == ["page-1", "page-2"]
    assert set(session.patches[0]["json"]["properties"]) == {
        "Tier", "Priority", "Score Reasons", "Qualified", "Lead Type", "Audit Notes",
    }
    assert "Runs Ads" not in session.patches[0]["json"]["properties"]  # manual value kept


def test_rescore_limit_caps_updates(tmp_path):
    sink, _ = _sink(tmp_path, [])
    sink.session = PatchSession()
    save_seen(sink.cache_path, {
        "domain:a.rs": {"page_id": "pa", "snapshot": {}},
        "domain:b.rs": {"page_id": "pb", "snapshot": {}},
    })
    report = sink.rescore([_v4_lead(domain="a.rs"), _v4_lead(domain="b.rs")], limit=1)
    assert len(report.updated) == 1 and report.pending == 1


def test_rescore_auth_failure_stops_the_batch(tmp_path):
    sink, _ = _sink(tmp_path, [])
    sink.session = PatchSession(patch_codes=[401])
    save_seen(sink.cache_path, {"domain:bobic.rs": {"page_id": "p", "snapshot": {}}})
    with pytest.raises(NotionAuthError):
        sink.rescore([_v4_lead()])


def test_draft_notes_written_at_creation_and_read_back():
    from pipeline.sinks.notion_sink import lead_from_page

    props = to_notion_properties(_lead(draft_message="Pozdrav?", draft_notes="check the slogan"))
    assert props["Draft Notes"]["rich_text"][0]["text"]["content"] == "check the slogan"
    assert "Draft Notes" not in to_notion_properties(_lead())
    page = _manual_page()
    page["properties"]["Draft Notes"] = {"rich_text": [{"plain_text": "check the slogan"}]}
    page["properties"]["Draft Message"] = {"rich_text": [{"plain_text": "old draft"}]}
    lead = lead_from_page(page)
    assert lead.draft_notes == "check the slogan"
    assert lead.draft_message == ""  # never read back: an old draft must not pass for a new one


# --- legacy-v1: collectable again, blocked by status / outcome / cooldown ---
TODAY = date(2026, 12, 1)


@pytest.mark.parametrize("info, reason", [
    ({}, None),
    ({"status": "Sent", "last_contacted": "2026-08-01"}, None),          # 122 days: past cooldown
    ({"status": "Sent", "last_contacted": "2026-09-02"}, "cooldown"),    # exactly 90 days: still blocked
    ({"status": "Sent", "last_contacted": "2026-09-01"}, None),          # 91 days
    ({"status": "Closed"}, "status"),
    ({"status": "Replied"}, "status"),
    ({"status": "Dead", "outcome": "declined"}, "status"),
    ({"outcome": "no_reply"}, None),
    *[({"outcome": o}, "status") for o in ("won", "replied", "meeting", "proposal")],
])
def test_legacy_block_reason(info, reason):
    assert legacy_block_reason(info, TODAY, 90) == reason


def test_legacy_info_reads_page():
    props = {"Status": {"select": {"name": "Sent"}}, "Outcome": {"select": None},
             "Last Contacted": {"date": {"start": "2026-10-31T10:00:00.000+01:00"}}}
    assert legacy_info(props) == {"status": "Sent", "outcome": "", "last_contacted": "2026-10-31"}


def test_blocking_seen_keeps_non_legacy_and_blocked_legacy():
    seen = {
        "domain:new.rs": {"page_id": "a", "campaign": "salon"},
        "domain:old-cache.rs": {"page_id": "b"},  # written before campaigns: still blocks
        "domain:free.rs": {"page_id": "c", "campaign": "legacy-v1", "legacy": {"status": "Sent"}},
        "domain:won.rs": {"page_id": "d", "campaign": "legacy-v1", "legacy": {"outcome": "won"}},
    }
    assert set(blocking_seen(seen, TODAY, 90)) == {"domain:new.rs", "domain:old-cache.rs", "domain:won.rs"}


def _legacy_seen(tmp_path, entries):
    save_seen(tmp_path / "seen_domains.json", entries)


def test_unblocked_legacy_business_gets_a_new_page_linking_the_old_one(tmp_path):
    sink, session = _sink(tmp_path, [FakeResponse(200, {"id": "new-page"})])
    _legacy_seen(tmp_path, {"domain:a.rs": {
        "page_id": "1111-2222", "campaign": "legacy-v1", "legacy": {"status": "Sent"},
        "snapshot": {"emails": [], "phones": [], "socials": {}, "domain": "a.rs", "company": "A"},
    }})
    report = sink.write([_lead("A", "a.rs", niche="frizerski salon")])
    assert [lead.company for lead in report.created] == ["A"] and len(report.relinked) == 1
    props = session.calls[0]["json"]["properties"]
    assert props["Campaign"] == {"select": {"name": "frizerski salon"}}
    assert props["Notes"]["rich_text"][0]["text"]["content"] == "legacy-v1: https://www.notion.so/11112222"
    entry = load_seen(sink.cache_path)["domain:a.rs"]
    assert entry["page_id"] == "new-page" and entry["campaign"] == "frizerski salon"
    assert entry["legacy_page_id"] == "1111-2222"
    # the business now blocks as a live page: a second sync is a cache hit
    assert sink.write([_lead("A", "a.rs", niche="frizerski salon")]).cached


def test_legacy_match_by_email_also_links(tmp_path):
    sink, session = _sink(tmp_path, [FakeResponse(200, {"id": "new-page"})])
    _legacy_seen(tmp_path, {"company:old name": {
        "page_id": "old", "campaign": "legacy-v1", "legacy": {},
        "snapshot": {"emails": ["info@a.rs"], "phones": [], "socials": {}, "domain": "", "company": "Old Name"},
    }})
    report = sink.write([_lead("A", "a.rs", contact_emails=["info@a.rs"], niche="salon")])
    assert len(report.relinked) == 1
    assert "legacy-v1: https://www.notion.so/old" in json.dumps(session.calls[0]["json"])
    seen = load_seen(sink.cache_path)
    assert seen["domain:a.rs"]["legacy_page_id"] == "old" and is_legacy(seen["company:old name"])


@pytest.mark.parametrize("legacy", [{"status": "Closed"}, {"outcome": "meeting"},
                                     {"status": "Sent", "last_contacted": date.today().isoformat()}])
def test_blocked_legacy_business_is_not_synced_or_commented(tmp_path, legacy):
    sink, session = _sink(tmp_path, [])
    _legacy_seen(tmp_path, {"domain:a.rs": {
        "page_id": "old", "campaign": "legacy-v1", "legacy": legacy,
        "snapshot": {"emails": [], "phones": [], "socials": {}, "domain": "a.rs", "company": "A"},
    }})
    report = sink.write([_lead("A", "a.rs", contact_emails=["new@a.rs"])])
    assert len(report.legacy_blocked) == 1 and not report.created and not report.cached
    assert session.calls == []  # no page, no comment on the legacy page


def test_refresh_cache_prefers_the_live_page_over_legacy(tmp_path):
    def page(page_id, campaign, **extra):
        props = {"Domain": {"url": "a.rs"}, "Company": {"title": [{"plain_text": "A"}]},
                 "Campaign": {"select": {"name": campaign} if campaign else None}, **extra}
        return {"id": page_id, "properties": props}

    legacy = page("old", "legacy-v1", Status={"select": {"name": "Closed"}})
    for order in ([legacy, page("new", "salon")], [page("new", "salon"), legacy]):
        sink, _ = _sink(tmp_path, [FakeResponse(200, {"results": order, "has_more": False})])
        sink.refresh_cache()
        entry = load_seen(sink.cache_path)["domain:a.rs"]
        assert (entry["page_id"], entry["campaign"], entry["legacy_page_id"]) == ("new", "salon", "old")
    sink, _ = _sink(tmp_path, [FakeResponse(200, {"results": [legacy], "has_more": False})])
    sink.refresh_cache()
    entry = load_seen(sink.cache_path)["domain:a.rs"]
    assert entry["campaign"] == "legacy-v1" and entry["legacy"]["status"] == "Closed"


def test_legacy_recontact_days_validated(tmp_path):
    from pipeline.config import ConfigError, load_config

    (tmp_path / "c.yaml").write_text("notion:\n  legacy_recontact_days: -1\n")
    with pytest.raises(ConfigError, match="legacy_recontact_days"):
        load_config(tmp_path / "c.yaml")
