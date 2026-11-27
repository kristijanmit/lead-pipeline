"""Shared v2 test config: market presets (config.yaml has no defaults for them)
and a niche definition. Niches never come from config — tests pass them in."""

MARKETS_YAML = """\
markets:
  serbia:
    country: Serbia
    dial_code: "+381"
    mobile_prefixes: ["+3816"]
    language: sr
    channels: [Instagram, Facebook, Viber/Whatsapp, Email, Call]
    maps_language: en
  uae:
    country: United Arab Emirates
    dial_code: "+971"
    mobile_prefixes: ["+9715"]
    language: en
    channels: [Instagram, Facebook, LinkedIn, Email, Call, WhatsApp]
    first_touch_excluded: [WhatsApp]
    review_strong: 100
    maps_language: en
"""

NICHE = {
    "name": "dental clinic",
    "search_terms": ["dentist", "dental clinic"],
    "phrase": "stomatološke ordinacije",
    "osm_tags": ["dentist"],
}

NICHE_YAML = """\
name: dental clinic
search_terms: [dentist, dental clinic]
phrase: stomatološke ordinacije
osm_tags: [dentist]
"""

SERBIA_BLOCK = {
    "id": "serbia", "country": "Serbia", "dial_code": "+381", "mobile_prefixes": ["+3816"],
    "language": "sr", "channels": ["Instagram", "Facebook", "Viber/Whatsapp", "Email", "Call"],
}


def _plain(prop: dict) -> dict:
    """An update payload as Notion would echo it back: rich_text gets plain_text."""
    if "rich_text" in prop:
        return {"rich_text": [{"plain_text": t.get("text", {}).get("content", t.get("plain_text", ""))}
                              for t in prop["rich_text"]]}
    return prop


def select(props: dict, name: str) -> str:
    return ((props.get(name) or {}).get("select") or {}).get("name", "")


def text(props: dict, name: str) -> str:
    return "".join(t.get("plain_text", "") for t in (props.get(name) or {}).get("rich_text", []))


class FakeNotion:
    """In-memory Notion for drafting tests. query_draftable applies the
    script_edits.md §11.2 filter (like NotionSink's server-side filter);
    update_page applies the update and records it. Never touches the network."""

    def __init__(self, cache_path):
        self.cache_path = str(cache_path)
        self.pages: dict[str, dict] = {}
        self.updates: list[tuple[str, dict]] = []
        self.queries: list[dict] = []

    def add(self, page_id, company, tier="B", status="Not started", market="serbia", industry="dental clinic",
            **extra):
        props = {
            "Company": {"title": [{"plain_text": company}]},
            "Qualified": {"checkbox": True},
            "Status": {"select": {"name": status}} if status else {"select": None},
            "Tier": {"select": {"name": tier}} if tier else {"select": None},
        }
        if market:
            props["Market"] = {"select": {"name": market}}
        if industry:
            props["Industry"] = {"select": {"name": industry}}
        props.update(extra)
        self.pages[page_id] = props
        return props

    def query_draftable(self, limit=None, redraft=False, min_tier="B", company="", page_ids=None):
        self.queries.append({"redraft": redraft, "min_tier": min_tier, "page_ids": page_ids})
        tiers = "ABCD"[: "ABCD".index(min_tier) + 1]
        statuses = ("Not started", "") + (("Queued", "Draft failed") if redraft else ())
        out = []
        for page_id, props in self.pages.items():
            if page_ids is not None and page_id not in page_ids:
                continue
            title = "".join(t.get("plain_text", "") for t in props["Company"]["title"])
            if (
                select(props, "Status") not in statuses
                or not props.get("Qualified", {}).get("checkbox")
                or select(props, "Verdict") == "Skip"
                or (select(props, "Tier") and select(props, "Tier") not in tiers)
                or (not redraft and text(props, "Draft Message"))
                or (company and company not in title)
            ):
                continue
            out.append({"id": page_id, "properties": props})
        return out[:limit] if limit is not None else out

    def query_pages(self):
        return [{"id": page_id, "properties": props} for page_id, props in self.pages.items()]

    def update_page(self, page_id, properties):
        self.updates.append((page_id, properties))
        self.pages[page_id].update({k: _plain(v) for k, v in properties.items()})
