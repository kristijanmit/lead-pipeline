"""One-off: tag every Notion page from before the 2026-11 niche/offer change as
Campaign = legacy-v1 (docs/ROADMAP.md, "Archived: legacy-v1").

A page is tagged only when its Campaign is empty AND its Date Added (Notion
created_time) is before --before. Pages synced since carry their niche as
Campaign, and the cutoff keeps a later re-run from ever tagging new pages
(hand-made ones included). Nothing is deleted; only Campaign is written.

Then data/seen_domains.json: every entry whose page is legacy-v1 gets
"campaign": "legacy-v1" and "legacy": {status, outcome, last_contacted}, so collect
and sync can let the business back in (notion_sink.blocking_seen): blocked while
the old page is Closed / Replied or has Outcome declined / won / replied /
meeting / proposal, or was contacted within notion.legacy_recontact_days. A copy
of the old cache goes to seen_domains.json.bak first.

NOT part of the pipeline (pipeline/runner.py never imports this). Dry run by
default: counts + 10 sample pages. --yes writes.

Usage:
    .venv/bin/python scripts/archive_legacy.py                 # dry run
    .venv/bin/python scripts/archive_legacy.py --yes           # write Campaign
    .venv/bin/python scripts/archive_legacy.py --before 2026-11-28T00:00+01:00
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import ConfigError, load_config
from pipeline.sinks.notion_sink import (
    LEGACY_CAMPAIGN, NotionAuthError, NotionSink, _select_name, legacy_block_reason, legacy_info, load_seen,
    save_seen,
)

DEFAULT_BEFORE = "2026-11-28T00:00+01:00"
SAMPLE = 10


def parse_cutoff(value: str) -> datetime:
    """ISO timestamp with a UTC offset — a naive one would be ambiguous."""
    cutoff = datetime.fromisoformat(value)
    if cutoff.tzinfo is None:
        raise ValueError(f"--before needs a UTC offset, e.g. {DEFAULT_BEFORE}")
    return cutoff


def date_added(page: dict) -> datetime | None:
    """Date Added is a created_time property; the page's own created_time is the
    same value and is used when the property is missing."""
    prop = page.get("properties", {}).get("Date Added") or {}
    raw = prop.get("created_time") or page.get("created_time")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None


def classify(pages: list[dict], cutoff: datetime) -> dict[str, list[dict]]:
    """Split pages into: to_tag (empty Campaign, added before cutoff), legacy
    (already legacy-v1), other_campaign, after_cutoff (empty Campaign, added at or
    after the cutoff, or no date)."""
    groups: dict[str, list[dict]] = {"to_tag": [], "legacy": [], "other_campaign": [], "after_cutoff": []}
    for page in pages:
        campaign = _select_name(page.get("properties", {}).get("Campaign"))
        added = date_added(page)
        if campaign == LEGACY_CAMPAIGN:
            groups["legacy"].append(page)
        elif campaign:
            groups["other_campaign"].append(page)
        elif added is None or added >= cutoff:
            groups["after_cutoff"].append(page)
        else:
            groups["to_tag"].append(page)
    return groups


def stamp_cache(seen: dict, legacy_pages: list[dict]) -> dict:
    """A copy of seen with every entry pointing at one of legacy_pages marked
    legacy-v1 and carrying that page's legacy_info. Other entries are unchanged."""
    by_id = {page["id"]: page for page in legacy_pages}
    out = {}
    for key, entry in seen.items():
        page = by_id.get(entry.get("page_id"))
        if page is not None:
            entry = {**entry, "campaign": LEGACY_CAMPAIGN, "legacy": legacy_info(page.get("properties", {}))}
        out[key] = entry
    return out


def block_counts(seen: dict, today: date, recontact_days: int) -> dict[str, int]:
    """Legacy-v1 cache entries by what collect/sync would do with them today."""
    counts = {"collectable": 0, "status": 0, "cooldown": 0}
    for entry in seen.values():
        if entry.get("campaign") == LEGACY_CAMPAIGN:
            reason = legacy_block_reason(entry.get("legacy", {}), today, recontact_days)
            counts[reason or "collectable"] += 1
    return counts


def describe(page: dict) -> str:
    props = page.get("properties", {})
    title = "".join(t.get("plain_text", "") for t in (props.get("Company") or {}).get("title", []))
    added = date_added(page)
    fields = [
        added.date().isoformat() if added else "no date",
        _select_name(props.get("Industry")) or "-",
        _select_name(props.get("Status")) or "-",
        f"Tier {_select_name(props.get('Tier')) or '-'}",
    ]
    return f"{title or '(untitled)'}  ({' · '.join(fields)})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--before", default=DEFAULT_BEFORE, help=f"Date Added cutoff (default {DEFAULT_BEFORE})")
    parser.add_argument("--yes", action="store_true", help="write Campaign (default: dry run)")
    args = parser.parse_args(argv)

    try:
        cutoff = parse_cutoff(args.before)
        config = load_config(args.config)
        sink = NotionSink(config)
    except (ValueError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    pages = sink.query_pages()
    groups = classify(pages, cutoff)
    to_tag = groups["to_tag"]
    mode = "WRITE" if args.yes else "DRY RUN"
    print(f"archive_legacy [{mode}] — Campaign = {LEGACY_CAMPAIGN} on pages with no Campaign "
          f"and Date Added before {cutoff.isoformat()}")
    print(f"  {len(pages)} pages read")
    print(f"  {len(to_tag)} to tag")
    print(f"  {len(groups['legacy'])} already {LEGACY_CAMPAIGN}")
    print(f"  {len(groups['other_campaign'])} with another Campaign — left alone")
    print(f"  {len(groups['after_cutoff'])} with no Campaign, added on/after the cutoff — left alone")

    days = config["notion"]["legacy_recontact_days"]
    seen = load_seen(sink.cache_path)
    planned = stamp_cache(seen, to_tag + groups["legacy"])
    counts = block_counts(planned, date.today(), days)
    stamped = sum(1 for e in planned.values() if e.get("campaign") == LEGACY_CAMPAIGN)
    print(f"  cache {sink.cache_path}: {len(seen)} entries, {stamped} legacy-v1 after this run")
    print(f"    {counts['collectable']} collectable again now")
    print(f"    {counts['status']} blocked by status / outcome (Closed, Replied; declined, won, replied, meeting, proposal)")
    print(f"    {counts['cooldown']} blocked by cooldown (Last Contacted within {days} days)")

    if not args.yes:
        for page in to_tag[:SAMPLE]:
            print(f"    {describe(page)}")
        if len(to_tag) > SAMPLE:
            print(f"    … and {len(to_tag) - SAMPLE} more")
        print("dry run — nothing written (pass --yes to write)")
        return 0

    failed: list[tuple[dict, str]] = []
    for n, page in enumerate(to_tag, 1):
        try:
            sink.update_page(page["id"], {"Campaign": {"select": {"name": LEGACY_CAMPAIGN}}})
        except NotionAuthError:
            raise
        except Exception as e:  # one bad page doesn't stop the rest
            failed.append((page, str(e)))
        if n % 25 == 0:
            print(f"  {n}/{len(to_tag)}")
    print(f"tagged {len(to_tag) - len(failed)} pages, {len(failed)} failed")
    for page, error in failed:
        print(f"  failed: {describe(page)}: {error}")

    # only pages that really are legacy-v1 now; a failed page stays a blocking entry
    failed_ids = {page["id"] for page, _ in failed}
    done = [page for page in to_tag if page["id"] not in failed_ids] + groups["legacy"]
    if sink.cache_path.exists():
        shutil.copy2(sink.cache_path, sink.cache_path.with_name(sink.cache_path.name + ".bak"))
    stamped_seen = stamp_cache(seen, done)
    save_seen(sink.cache_path, stamped_seen)
    n = sum(1 for e in stamped_seen.values() if e.get("campaign") == LEGACY_CAMPAIGN)
    print(f"cache: {n} entries marked {LEGACY_CAMPAIGN} (backup: {sink.cache_path.name}.bak)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
