"""One-off migration helper — turns Markdown-looking text in the Notion
"Social Links" property (`[Facebook](https://...)`, which Notion shows
literally) into real clickable links, on pages that already exist.

Touches ONLY the Social Links property, ONLY on pages whose value still has no
link objects. Defaults to a dry run; pass --yes to write.

Usage:
    .venv/bin/python scripts/fix_social_links.py          # dry run
    .venv/bin/python scripts/fix_social_links.py --yes    # write
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from pipeline.config import ConfigError, load_config, load_env
from pipeline.sinks.notion_sink import social_links_rich_text

API_BASE = "https://api.notion.com/v1"
_MD_LINK = re.compile(r"\\?\[([^\]\\]+)\\?\]\(([^)\s]+)\)")


def links_from_text(text: str) -> dict[str, str]:
    return {name.lower(): url for name, url in _MD_LINK.findall(text)}


def needs_fix(prop: dict | None) -> dict[str, str]:
    """Links to rebuild, or {} when the page is empty or already has real links."""
    items = (prop or {}).get("rich_text", [])
    if not items or any(i.get("href") for i in items):
        return {}
    return links_from_text("".join(i.get("plain_text", "") for i in items))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--yes", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    notion, env = config["notion"], load_env()
    database_id = notion["database_id"] or env.get("NOTION_DATABASE_ID", "")
    token = env.get("NOTION_TOKEN", "")
    if not database_id or not token:
        print("error: NOTION_TOKEN / NOTION_DATABASE_ID not set (see README Notion setup)", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": notion["api_version"],
        "Content-Type": "application/json",
    }
    session = requests.Session()
    pages, cursor = [], None
    while True:
        payload = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        r = session.post(f"{API_BASE}/databases/{database_id}/query", headers=headers, json=payload, timeout=notion["timeout_s"])
        r.raise_for_status()
        data = r.json()
        pages += data.get("results", [])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    todo = [(p, links) for p in pages if (links := needs_fix(p["properties"].get("Social Links")))]
    print(f"{len(pages)} pages, {len(todo)} need a Social Links fix")
    for page, links in todo:
        print(f"  {'fix' if args.yes else 'would fix'}: {page['id']} -> {', '.join(sorted(links))}")
        if args.yes:
            r = session.patch(
                f"{API_BASE}/pages/{page['id']}",
                headers=headers,
                json={"properties": {"Social Links": social_links_rich_text(links)}},
                timeout=notion["timeout_s"],
            )
            r.raise_for_status()
            time.sleep(notion["delay_s"])
    if todo and not args.yes:
        print("\ndry run — pass --yes to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
