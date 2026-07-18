"""One-off migration helper — archives every page in the Notion leads
database and clears the local seen_domains.json cache, so a fresh
`score` + `sync` can repopulate the database under the new scoring schema
(Reachability Score / Opportunity Score / Qualified) instead of trying to
patch 44 existing rows in place.

NOT part of the ongoing pipeline (pipeline/runner.py never imports this).
Run by hand, once, right after fixing the Notion database's column labels
by hand in the UI (rename Website Audit Score -> Opportunity Score, delete
ICP Fit Score and Intent Score, add Reachability Score + Qualified).

THIS DELETES LIVE DATA IN A SHARED SYSTEM. Defaults to a dry run — it only
lists what it *would* archive. Pass --yes to actually archive.

Usage:
    .venv/bin/python scripts/clear_notion_pages.py            # dry run
    .venv/bin/python scripts/clear_notion_pages.py --yes       # actually archive + clear cache
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from pipeline.config import ConfigError, load_config, load_env

API_BASE = "https://api.notion.com/v1"


def _title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return "(untitled)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--yes", action="store_true", help="actually archive pages and clear the cache (default: dry run)"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    notion = config["notion"]
    if not notion["database_id"]:
        print("error: notion.database_id is not set in config.yaml", file=sys.stderr)
        return 1
    token = load_env().get("NOTION_TOKEN", "")
    if not token:
        print("error: NOTION_TOKEN is not set (see README Notion setup)", file=sys.stderr)
        return 1

    session = requests.Session()
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": notion["api_version"],
        "Content-Type": "application/json",
    }

    pages = []
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        response = session.post(
            f"{API_BASE}/databases/{notion['database_id']}/query",
            headers=headers,
            json=payload,
            timeout=notion["timeout_s"],
        )
        response.raise_for_status()
        data = response.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    print(f"found {len(pages)} page(s) in database {notion['database_id']}")
    for page in pages:
        print(f"  {'archive' if args.yes else 'would archive'}: {_title(page)}")

    if not args.yes:
        print("\ndry run — pass --yes to actually archive these pages and clear the cache")
        return 0

    for page in pages:
        response = session.patch(
            f"{API_BASE}/pages/{page['id']}",
            headers=headers,
            json={"archived": True},
            timeout=notion["timeout_s"],
        )
        response.raise_for_status()
        if notion["delay_s"]:
            time.sleep(notion["delay_s"])

    cache_path = Path(notion["cache_path"])
    if cache_path.exists():
        cache_path.unlink()
        print(f"cleared {cache_path}")

    print(f"archived {len(pages)} page(s) — run a full `score` + `sync` to repopulate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
