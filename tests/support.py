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
