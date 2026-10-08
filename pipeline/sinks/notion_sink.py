"""Notion sink — push scored leads into the AGENCY leads database.

The Lead -> Notion property mapping lives in one function,
to_notion_properties(), per docs/ARCHITECTURE.md §6.1. Idempotency comes from a
local seen_domains.json cache keyed by the string form of dedupe.lead_key,
plus a secondary email index built from each entry's content snapshot (the
same business collected once before its domain was known and once after
still matches) — never a live Notion query per lead (§9). A cache hit whose
lead carries info not yet on the existing page gets that info added as a
Notion comment rather than silently dropped. Per-lead API failures land in
the returned SyncReport, they don't halt the batch; auth failures raise
immediately since no lead in the batch can succeed.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

from pipeline.config import load_env
from pipeline.dedupe import email_keys, lead_key, normalize_domain, phone_key
from pipeline.schema import Lead
from pipeline.sinks.base import register

API_BASE = "https://api.notion.com/v1"

# pipeline source values -> existing Notion select options; unknown values
# pass through raw and Notion auto-creates an option (same as Industry/CMS)
_SOURCE_OPTIONS = {"osm": "OSM/Maps", "maps": "OSM/Maps", "manual": "Manual"}

# Lead.lighthouse key -> label in the single "Lighthouse" text property
_LIGHTHOUSE_LABELS = {
    "performance": "Performance",
    "accessibility": "Accessibility",
    "best_practices": "Best Practices",
    "seo": "SEO",
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
        "Status": {"select": {"name": "Not started"}},
        "Qualified": {"checkbox": lead.qualified},
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
    if lead.address:
        props["Address"] = _text(lead.address)
    if lead.rating is not None:
        props["Rating"] = {"number": lead.rating}
    if lead.review_count is not None:
        props["Review Count"] = {"number": lead.review_count}
    if lead.profile_url:
        props["Profile URL"] = {"url": lead.profile_url}
    if lead.https is not None:
        props["HTTPS"] = {"checkbox": lead.https}
    if lead.mobile_friendly is not None:
        props["Mobile Friendly"] = {"checkbox": lead.mobile_friendly}
    if lead.cms:
        props["CMS"] = {"select": {"name": lead.cms}}
    if lead.lighthouse:
        lines = [
            f"{label}: {lead.lighthouse[key]}"
            for key, label in _LIGHTHOUSE_LABELS.items()
            if key in lead.lighthouse
        ]
        if lines:
            props["Lighthouse"] = _text("\n".join(lines))
    if lead.audit_notes:
        props["Audit Notes"] = _text(lead.audit_notes)
    if lead.reachability_score is not None:
        props["Reachability Score"] = {"number": lead.reachability_score}
    if lead.opportunity_score is not None:
        props["Opportunity Score"] = {"number": lead.opportunity_score}
    if lead.total_score is not None:
        props["Total Score"] = {"number": lead.total_score}
    if lead.lead_type:
        props["Lead Type"] = {"select": {"name": lead.lead_type}}
    return props


def cache_key(lead: Lead) -> str:
    """String form of dedupe.lead_key — "domain:example.com" / "company:foo"."""
    kind, value = lead_key(lead)
    return f"{kind}:{value}"


def _snapshot(lead: Lead) -> dict:
    """Content snapshot stored alongside a cache entry — supports the email
    index and new-info diffing below without a live Notion query per lead."""
    return {
        "emails": lead.contact_emails,
        "phones": lead.contact_phones,
        "socials": lead.social_links,
        "domain": lead.domain,
        "company": lead.company,
    }


def _email_index(seen: dict) -> dict[str, str]:
    """Lowercased email -> cache key, built from every entry's snapshot."""
    index: dict[str, str] = {}
    for key, entry in seen.items():
        for email in entry.get("snapshot", {}).get("emails", []):
            index.setdefault(email.strip().lower(), key)
    return index


def _merge_snapshot(snapshot: dict, lead: Lead) -> dict:
    """Fold a matched lead's contact info into an existing cache snapshot."""
    return {
        "emails": list({*snapshot.get("emails", []), *lead.contact_emails}),
        "phones": list({*snapshot.get("phones", []), *lead.contact_phones}),
        "socials": {**snapshot.get("socials", {}), **lead.social_links},
        "domain": snapshot.get("domain") or lead.domain,
        "company": snapshot.get("company") or lead.company,
    }


def _new_info(snapshot: dict, lead: Lead) -> list[str]:
    """Human-readable lines for anything lead has that snapshot doesn't."""
    lines = []
    known_emails = {e.lower() for e in snapshot.get("emails", [])}
    new_emails = [e for e in lead.contact_emails if e.lower() not in known_emails]
    if new_emails:
        lines.append(f"Additional email(s): {', '.join(new_emails)}")
    known_phones = {phone_key(p) for p in snapshot.get("phones", [])}
    new_phones = [p for p in lead.contact_phones if phone_key(p) not in known_phones]
    if new_phones:
        lines.append(f"Additional phone(s): {', '.join(new_phones)}")
    known_socials = snapshot.get("socials", {})
    new_socials = {
        k: v for k, v in lead.social_links.items() if known_socials.get(k) != v
    }
    for platform, url in new_socials.items():
        lines.append(f"New social link — {platform}: {url}")
    return lines


def _rich_text(prop: dict | None) -> str:
    if not prop:
        return ""
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))


def _snapshot_from_properties(props: dict) -> dict:
    """Reverse of to_notion_properties() for the fields the cache needs —
    used by refresh_cache() to rebuild snapshots from what's live in Notion."""
    emails = []
    primary_email = (props.get("Primary Email") or {}).get("email")
    if primary_email:
        emails.append(primary_email)
    emails += [e.strip() for e in _rich_text(props.get("Additional Emails")).split(",") if e.strip()]

    phones = []
    primary_phone = (props.get("Phone") or {}).get("phone_number")
    if primary_phone:
        phones.append(primary_phone)
    phones += [p.strip() for p in _rich_text(props.get("Additional Phones")).split(",") if p.strip()]

    socials = {
        platform.lower(): url
        for platform, url in re.findall(r"\[(\w+)\]\(([^)]+)\)", _rich_text(props.get("Social Links")))
    }

    title = (props.get("Company") or {}).get("title") or []
    company = "".join(t.get("plain_text", "") for t in title).strip()

    return {
        "emails": emails,
        "phones": phones,
        "socials": socials,
        "domain": normalize_domain((props.get("Domain") or {}).get("url")),
        "company": company,
    }


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
        env = load_env()
        self.database_id = notion["database_id"] or env.get("NOTION_DATABASE_ID", "")
        if not self.database_id:
            raise ValueError(
                "notion.database_id is not set in config.yaml or NOTION_DATABASE_ID "
                "in .env — see README Notion setup"
            )
        self.delay_s = notion["delay_s"]
        self.timeout_s = notion["timeout_s"]
        self.cache_path = Path(notion["cache_path"])
        token = token or env.get("NOTION_TOKEN", "")
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

        A lead matches an existing entry either by cache_key (domain or
        company) or by sharing an email with one already synced — the same
        business collected once before its domain resolved and once after
        must not become two pages. A match whose lead carries info the
        existing page doesn't get that info added as a comment instead of
        being silently dropped.

        The cache entry is written after each successful create (or update),
        so a crash mid-batch stays idempotent — the re-run skips exactly
        what landed.
        """
        seen = load_seen(self.cache_path)
        email_index = _email_index(seen)
        report = SyncReport()
        attempts = 0
        for lead in leads:
            key = cache_key(lead)
            match_key = key if key in seen else next(
                (email_index[e] for e in email_keys(lead) if e in email_index), None
            )
            if match_key is not None:
                entry = seen[match_key]
                new_info = _new_info(entry.get("snapshot", {}), lead)
                if new_info:
                    try:
                        self._create_comment(
                            entry["page_id"],
                            "New info from a later sync:\n"
                            + "\n".join(f"- {line}" for line in new_info),
                        )
                    except NotionAuthError:
                        raise
                    except Exception as e:
                        report.failed.append((lead, f"comment failed: {e}"))
                entry["snapshot"] = _merge_snapshot(entry.get("snapshot", {}), lead)
                save_seen(self.cache_path, seen)
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
                "snapshot": _snapshot(lead),
            }
            for email in email_keys(lead):
                email_index[email] = key
            save_seen(self.cache_path, seen)
            report.created.append(lead)
        return report

    def refresh_cache(self) -> int:
        """Rebuild seen_domains.json from what's actually in Notion — the
        periodic refresh from docs/ARCHITECTURE.md §9. Replaces the cache: Notion
        is the authority on which pages exist. Also rebuilds each entry's
        content snapshot (from the same properties to_notion_properties()
        writes) so email matching and new-info diffing keep working after
        a refresh."""
        seen: dict = {}
        cursor = None
        while True:
            payload: dict = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self._post(f"{API_BASE}/databases/{self.database_id}/query", payload)
            for page in data.get("results", []):
                snapshot = _snapshot_from_properties(page.get("properties", {}))
                if snapshot["domain"]:
                    key = f"domain:{snapshot['domain']}"
                elif snapshot["company"]:
                    key = f"company:{snapshot['company'].lower()}"
                else:
                    continue  # untitled page — nothing to key on
                seen.setdefault(
                    key, {"page_id": page["id"], "synced_at": None, "snapshot": snapshot}
                )
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

    def _create_comment(self, page_id: str, text: str) -> None:
        self._post(
            f"{API_BASE}/comments",
            {"parent": {"page_id": page_id}, "rich_text": [{"text": {"content": text}}]},
        )

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
