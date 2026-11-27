"""profile_facts — short, checkable facts the drafter may praise when the operator
wrote no SM Audit note (script_edits.md §11.3).

Pure: built from the homepage HTML the contact enricher already fetched plus the
Maps fields collect kept. No requests. The drafter calls it without HTML for leads
enriched before schema v14, so it must work from the Lead alone too.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from pipeline.schema import Lead

MAX_FACTS = 5
MAX_CHARS = 80

# navigation / boilerplate headings (sr + en) that say nothing about the business;
# compared after lowercasing, stripping punctuation and diacritics
_GENERIC = {
    "kontakt", "kontaktirajte nas", "kontakt informacije", "o nama", "usluge", "nase usluge", "pocetna",
    "galerija", "blog", "vesti", "novosti", "cenovnik", "cene", "tim", "nas tim", "utisci", "lokacija",
    "radno vreme", "zakazite", "zakazite termin", "saznajte vise", "procitajte vise", "pratite nas",
    "politika privatnosti", "uslovi koriscenja", "pretraga", "meni", "mapa sajta", "zasto mi",
    "contact", "contact us", "get in touch", "about", "about us", "home", "services", "our services",
    "gallery", "news", "prices", "pricing", "faq", "team", "our team", "testimonials", "reviews",
    "location", "opening hours", "book now", "book an appointment", "read more", "learn more",
    "follow us", "privacy policy", "terms", "terms and conditions", "newsletter", "login", "search",
    "menu", "sitemap", "why us", "welcome", "dobrodosli",
}
_GENERIC_PARTS = ("cookie", "kolacic", "kolačić", "javascript", "wordpress", "page not found", "404")

_TRANSLIT = str.maketrans("čćšžđ", "ccszd")


def _key(text: str) -> str:
    text = text.lower().translate(_TRANSLIT)
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def _clip(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_CHARS:
        return text
    cut = text[: MAX_CHARS - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "…"


# headings that open with these are navigation or a sales pitch, whatever follows
# ("Zašto odabrati Dental Spa", "Welcome to …")
_GENERIC_STARTS = ("zasto odabrati", "zasto izabrati", "zasto bas", "why choose", "dobrodosli", "welcome to")
# "O nama – Dental Spa Šabac" / "Contact | Brand": the first part decides
_SEGMENT_RE = re.compile(r"\s+[–—|-]\s+|\s*\|\s*|:\s+")


def _generic(text: str) -> bool:
    key = _key(text)
    first = _key(_SEGMENT_RE.split(text, maxsplit=1)[0])
    return (
        len(key.replace(" ", "")) < 3
        or key in _GENERIC
        or first in _GENERIC
        or key.startswith(_GENERIC_STARTS)
        or any(part in text.lower() or part in key for part in _GENERIC_PARTS)
    )


def _site_parts(html: str) -> list[tuple[str, str]]:
    """(label, text) from the homepage: title, meta description, h1-h3 headings."""
    soup = BeautifulSoup(html, "html.parser")
    parts: list[tuple[str, str]] = []
    if soup.title and soup.title.string:
        parts.append(("Website title", soup.title.string))
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        parts.append(("Website description", str(meta["content"])))
    for tag in soup.find_all(["h1", "h2", "h3"]):
        parts.append(("Named on the website", tag.get_text(" ", strip=True)))
    return parts


def build_profile_facts(lead: Lead, html: str = "") -> list[str]:
    """At most MAX_FACTS facts of at most ~MAX_CHARS chars, most praise-worthy first:
    Maps description, site description, site headings (services), site title, Maps
    category. Generic headings, the bare company name and duplicates are dropped."""
    site = _site_parts(html) if html else []
    candidates: list[tuple[str, str]] = []
    if lead.maps_description:
        candidates.append(("Google Maps description", lead.maps_description))
    candidates += [p for p in site if p[0] == "Website description"]
    candidates += [p for p in site if p[0] == "Named on the website"]
    candidates += [p for p in site if p[0] == "Website title"]
    if lead.maps_category:
        candidates.append(("Google Maps category", lead.maps_category))

    company = _key(lead.company)
    seen: set[str] = set()
    facts: list[str] = []
    for label, text in candidates:
        text = " ".join(str(text).split())
        key = _key(text)
        if not key or _generic(text) or key == company or key in seen:
            continue
        seen.add(key)
        facts.append(_clip(f"{label}: {text}"))
        if len(facts) == MAX_FACTS:
            break
    return facts
