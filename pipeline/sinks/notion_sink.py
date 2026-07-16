"""Notion sink — push scored leads into the AGENCY leads database.

The Lead -> Notion property mapping lives in one function,
to_notion_properties(), per ARCHITECTURE.md §6.1. Idempotency comes from a
local seen_domains.json cache keyed by the string form of dedupe.lead_key —
never a live Notion query per lead (§9). Per-lead API failures land in the
returned SyncReport, they don't halt the batch; auth failures raise
immediately since no lead in the batch can succeed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from pipeline.config import load_env
from pipeline.dedupe import lead_key, normalize_domain
from pipeline.schema import Lead
from pipeline.sinks.base import register

API_BASE = "https://api.notion.com/v1"

# pipeline source values -> existing Notion select options; unknown values
# pass through raw and Notion auto-creates an option (same as Industry/CMS)
_SOURCE_OPTIONS = {"osm": "OSM/Maps", "maps": "OSM/Maps", "manual": "Manual"}

_LIGHTHOUSE_COLUMNS = {
    "performance": "Performance Score",
    "accessibility": "Accessibility Score",
    "best_practices": "Best Practices Score",
    "seo": "SEO Score",
}


class NotionAuthError(RuntimeError):
    """401/403 from Notion — the whole batch would fail, so stop at once."""


@dataclass
class SyncReport:
    created: list[Lead] = field(default_factory=list)
    cached: list[Lead] = field(default_factory=list)  # already in Notion
    failed: list[tuple[Lead, str]] = field(default_factory=list)
    pending: int = 0  # beyond --limit, untouched


def _text(content: str) -> dict:
    return {"rich_text": [{"text": {"content": content}}]}


def to_notion_properties(lead: Lead) -> dict:
    """The single §6.1 mapping point. None/empty fields are omitted rather
    than sent as blanks — a checkbox can't say "unknown", so HTTPS/Mobile
    Friendly only appear when the audit actually determined them.
    schema_version/scoring_version/run_id/errors are deliberately not
    synced: bookkeeping stays in the JSONL where it's queryable."""
    props: dict = {
        "Company": {"title": [{"text": {"content": lead.company}}]},
        "Industry": {"select": {"name": lead.industry}},
        "Source": {"select": {"name": _SOURCE_OPTIONS.get(lead.source, lead.source)}},
        "Status": {"status": {"name": "Not started"}},
        "Intent Score": {"number": lead.intent_score},
    }
    if lead.location:
        props["Location"] = {"select": {"name": lead.location}}
    if lead.domain:
        props["Domain"] = {"url": lead.domain}
    if lead.contact_emails:
        props["Primary Email"] = {"email": lead.contact_emails[0]}
        if lead.contact_emails[1:]:
            props["Additional Emails"] = _text(", ".join(lead.contact_emails[1:]))
    if lead.contact_phones:
        props["Phone"] = {"phone_number": lead.contact_phones[0]}
        if lead.contact_phones[1:]:
            props["Additional Phones"] = _text(", ".join(lead.contact_phones[1:]))
    if lead.social_links:
        lines = [
            f"[{platform.capitalize()}]({url})"
            for platform, url in sorted(lead.social_links.items())
        ]
        props["Social Links"] = _text("\n".join(lines))
    if lead.https is not None:
        props["HTTPS"] = {"checkbox": lead.https}
    if lead.mobile_friendly is not None:
        props["Mobile Friendly"] = {"checkbox": lead.mobile_friendly}
    if lead.cms:
        props["CMS"] = {"select": {"name": lead.cms}}
    for key, column in _LIGHTHOUSE_COLUMNS.items():
        if lead.lighthouse and key in lead.lighthouse:
            props[column] = {"number": lead.lighthouse[key]}
    if lead.audit_notes:
        props["Audit Notes"] = _text(lead.audit_notes)
    if lead.icp_fit_score is not None:
        props["ICP Fit Score"] = {"number": lead.icp_fit_score}
    if lead.website_audit_score is not None:
        props["Website Audit Score"] = {"number": lead.website_audit_score}
    if lead.total_score is not None:
        props["Total Score"] = {"number": lead.total_score}
    if lead.lead_type:
        props["Lead Type"] = {"select": {"name": lead.lead_type}}
    return props


def cache_key(lead: Lead) -> str:
    """String form of dedupe.lead_key — "domain:example.com" / "company:foo"."""
    kind, value = lead_key(lead)
    return f"{kind}:{value}"


def load_seen(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_seen(path: str | Path, seen: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(seen, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@register("notion")
class NotionSink:
    def __init__(self, config: dict, token: str | None = None, session=None):
        notion = config["notion"]
        if not notion["database_id"]:
            raise ValueError(
                "notion.database_id is not set in config.yaml — see README Notion setup"
            )
        self.database_id = notion["database_id"]
        self.delay_s = notion["delay_s"]
        self.timeout_s = notion["timeout_s"]
        self.cache_path = Path(notion["cache_path"])
        token = token or load_env().get("NOTION_TOKEN", "")
        if not token:
            raise ValueError(
                "NOTION_TOKEN is not set — create an internal integration at "
                "notion.so/my-integrations, connect it to the leads database, "
                "and put the token in .env (see README Notion setup)"
            )
        self.session = session if session is not None else requests.Session()
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": notion["api_version"],
            "Content-Type": "application/json",
        }

    def write(self, leads: list[Lead], limit: int | None = None) -> SyncReport:
        """Create one Notion page per lead not already in the cache.

        The cache entry is written after each successful create, so a crash
        mid-batch stays idempotent — the re-run skips exactly what landed.
        """
        seen = load_seen(self.cache_path)
        report = SyncReport()
        attempts = 0
        for lead in leads:
            key = cache_key(lead)
            if key in seen:
                report.cached.append(lead)
                continue
            # limit caps attempts, not successes — a dry run against a broken
            # setup must not burn through the whole batch failing lead by lead
            if limit is not None and attempts >= limit:
                report.pending += 1
                continue
            if attempts and self.delay_s:
                time.sleep(self.delay_s)
            attempts += 1
            try:
                page_id = self._create_page(lead)
            except NotionAuthError:
                raise
            except Exception as e:
                report.failed.append((lead, str(e)))
                continue
            seen[key] = {
                "page_id": page_id,
                "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            save_seen(self.cache_path, seen)
            report.created.append(lead)
        return report

    def refresh_cache(self) -> int:
        """Rebuild seen_domains.json from what's actually in Notion — the
        periodic refresh from ARCHITECTURE.md §9. Replaces the cache: Notion
        is the authority on which pages exist."""
        seen: dict = {}
        cursor = None
        while True:
            payload: dict = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._post(f"{API_BASE}/databases/{self.database_id}/query", payload)
            for page in data.get("results", []):
                props = page.get("properties", {})
                domain = normalize_domain((props.get("Domain") or {}).get("url"))
                if domain:
                    key = f"domain:{domain}"
                else:
                    title = (props.get("Company") or {}).get("title") or []
                    company = "".join(
                        t.get("plain_text", "") for t in title
                    ).strip().lower()
                    if not company:
                        continue  # untitled page — nothing to key on
                    key = f"company:{company}"
                seen.setdefault(key, {"page_id": page["id"], "synced_at": None})
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        save_seen(self.cache_path, seen)
        return len(seen)

    def _create_page(self, lead: Lead) -> str:
        payload = {
            "parent": {"database_id": self.database_id},
            "properties": to_notion_properties(lead),
        }
        return self._post(f"{API_BASE}/pages", payload)["id"]

    def _post(self, url: str, payload: dict) -> dict:
        response = self.session.post(
            url, headers=self._headers, json=payload, timeout=self.timeout_s
        )
        if response.status_code == 429 or response.status_code >= 500:
            # one retry honoring Retry-After — transient by definition
            time.sleep(float(response.headers.get("Retry-After", 1)))
            response = self.session.post(
                url, headers=self._headers, json=payload, timeout=self.timeout_s
            )
        if response.status_code in (401, 403):
            raise NotionAuthError(
                f"Notion auth failed ({response.status_code}) — check NOTION_TOKEN "
                "and that the integration is connected to the database"
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Notion API {response.status_code}: {response.text[:200]}")
        return response.json()
