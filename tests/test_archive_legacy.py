import importlib.util
from datetime import timezone
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "archive_legacy", Path(__file__).resolve().parent.parent / "scripts" / "archive_legacy.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def page(page_id, created, campaign=None):
    props = {"Company": {"title": [{"plain_text": page_id}]}, "Date Added": {"created_time": created}}
    props["Campaign"] = {"select": {"name": campaign} if campaign else None}
    return {"id": page_id, "created_time": created, "properties": props}


CUTOFF = mod.parse_cutoff(mod.DEFAULT_BEFORE)  # 2026-11-28 00:00 Belgrade = 2026-11-27 23:00 UTC


def ids(pages):
    return [p["id"] for p in pages]


def test_only_empty_campaign_before_cutoff_is_tagged():
    groups = mod.classify([
        page("old", "2026-07-16T20:30:00.000Z"),
        page("just-before", "2026-11-27T22:59:00.000Z"),
        page("at-cutoff", "2026-11-27T23:00:00.000Z"),
        page("new", "2026-12-02T10:00:00.000Z"),
        page("tagged", "2026-07-16T20:30:00.000Z", "legacy-v1"),
        page("other", "2026-07-16T20:30:00.000Z", "dental clinic"),
    ], CUTOFF)
    assert ids(groups["to_tag"]) == ["old", "just-before"]
    assert ids(groups["after_cutoff"]) == ["at-cutoff", "new"]
    assert ids(groups["legacy"]) == ["tagged"]
    assert ids(groups["other_campaign"]) == ["other"]


def test_rerun_tags_nothing_new():
    pages = [page("old", "2026-07-16T20:30:00.000Z", "legacy-v1"), page("new", "2026-12-02T10:00:00.000Z")]
    assert mod.classify(pages, CUTOFF)["to_tag"] == []


def test_page_without_date_is_never_tagged():
    p = {"id": "x", "properties": {"Campaign": {"select": None}}}
    assert ids(mod.classify([p], CUTOFF)["after_cutoff"]) == ["x"]


def test_cutoff_needs_an_offset():
    assert CUTOFF.astimezone(timezone.utc).isoformat() == "2026-11-27T23:00:00+00:00"
    with pytest.raises(ValueError):
        mod.parse_cutoff("2026-11-28T00:00")


def test_stamp_cache_marks_legacy_entries_and_counts_blocks():
    from datetime import date

    pages = [
        {"id": "p1", "properties": {"Status": {"select": {"name": "Closed"}}}},
        {"id": "p2", "properties": {"Last Contacted": {"date": {"start": "2026-11-20"}}}},
        {"id": "p3", "properties": {"Status": {"select": {"name": "Sent"}}}},
    ]
    seen = {"domain:a.rs": {"page_id": "p1"}, "domain:b.rs": {"page_id": "p2"},
            "domain:c.rs": {"page_id": "p3"}, "domain:new.rs": {"page_id": "p9", "campaign": "salon"}}
    stamped = mod.stamp_cache(seen, pages)
    assert stamped["domain:a.rs"]["campaign"] == "legacy-v1"
    assert stamped["domain:b.rs"]["legacy"]["last_contacted"] == "2026-11-20"
    assert stamped["domain:new.rs"] == {"page_id": "p9", "campaign": "salon"}
    assert "campaign" not in seen["domain:a.rs"]  # input untouched
    assert mod.block_counts(stamped, date(2026, 12, 1), 90) == {"collectable": 1, "status": 1, "cooldown": 1}
