import importlib.util
from pathlib import Path

from pipeline.schema import Lead

spec = importlib.util.spec_from_file_location(
    "backfill_drafts", Path(__file__).resolve().parent.parent / "scripts" / "backfill_drafts.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _page(draft="", status="Not started"):
    return {"properties": {
        "Draft Message": {"rich_text": [{"plain_text": draft}] if draft else []},
        "Status": {"select": {"name": status}},
    }}


def _lead():
    return Lead(company="A", domain="a.rs", source="osm", qualified=True,
                suggested_channel="Instagram", draft_message="Dobar dan")


def test_fills_empty_page_and_queues_not_started():
    u = mod.plan_update(_lead(), _page())
    assert u["Suggested Channel"] == {"select": {"name": "Instagram"}}
    assert u["Draft Message"]["rich_text"][0]["text"]["content"] == "Dobar dan"
    assert u["Status"] == {"select": {"name": "Queued"}}


def test_never_overwrites_an_existing_draft():
    assert mod.plan_update(_lead(), _page(draft="mine")) is None


def test_leaves_status_alone_unless_not_started():
    assert "Status" not in mod.plan_update(_lead(), _page(status="Sent"))
