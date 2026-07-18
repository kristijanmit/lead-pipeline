from pathlib import Path

from pipeline.enrichers.contact import (
    ContactEnricher,
    extract_emails,
    extract_phones,
    extract_social_links,
    find_contact_url,
)
from pipeline.schema import Lead

FIXTURES = Path(__file__).parent / "fixtures"

HTML = (FIXTURES / "contact_page.html").read_text(encoding="utf-8")

CONFIG = {"enrich": {"http_timeout_s": 20, "lighthouse": {"binary": "lighthouse", "timeout_s": 120}}}


def test_extract_emails_keeps_every_address():
    emails = extract_emails(HTML)
    # mailto (with ?subject stripped), plain text, and entity-obfuscated
    assert emails == [
        "info@dentalnovak.rs",
        "ordinacija@dentalnovak.rs",
        "marketing@dentalnovak.rs",
    ]


def test_extract_emails_filters_assets_and_placeholders():
    emails = extract_emails(HTML)
    assert "logo@2x.png" not in emails  # asset filename, not an inbox
    assert "primer@example.com" not in emails  # placeholder domain


def test_extract_phones_keeps_every_number_deduped_across_formats():
    phones = extract_phones(HTML)
    # tel: href first; its anchor text, and "021/123-45-67" (+381 21 = 021),
    # are the same number in other spellings and must not double up
    assert phones == ["+381211234567", "060 555 333 4"]


def test_tel_scheme_slash_variant_stripped():
    # seen live on dentalbobic.com: tel://+381... left "//" on the number
    assert extract_phones('<a href="tel://+381 63 821 7839">zovi</a>') == [
        "+381 63 821 7839"
    ]


def test_extract_phones_skips_years_and_ids():
    phones = extract_phones(HTML)
    assert all("2004" not in p and "106111222" not in p for p in phones)


def test_extract_phones_rejects_date_shaped_strings():
    # each seen live on the Notion board, misparsed as a phone number
    dates = (
        "06/07/2026",
        "05/11/2025, 07/12/2023, 01 2023-12-07 13",
        "05.10.2026, 05.11. 2026",
        "02.06.2018",
    )
    for text in dates:
        assert extract_phones(f"<p>Radno vreme: {text}</p>") == [], text


def test_extract_phones_keeps_slash_separated_local_numbers():
    # Serbian city-code/number format also uses "/" — must not be
    # collapsed into the date-rejection fix above
    assert extract_phones("<p>Pozovite 021/4879-819 ili 065/ 53-63-847</p>") == [
        "021/4879-819",
        "065/ 53-63-847",
    ]


def test_extract_social_links():
    links = extract_social_links(HTML)
    assert links == {
        "facebook": "https://www.facebook.com/dentalnovak",
        "instagram": "https://www.instagram.com/dental.novak/",
        "linkedin": "https://www.linkedin.com/company/dental-centar-novak",
        "x": "https://x.com/dentalnovak",
    }


def test_find_contact_url_resolves_relative_href():
    url = find_contact_url(HTML, "https://dentalnovak.rs/")
    assert url == "https://dentalnovak.rs/kontakt"


def test_merge_keeps_collector_values_first():
    class OnePageEnricher(ContactEnricher):
        def _fetch_homepage(self, domain):
            return (f"https://{domain}/", HTML)

        def _fetch(self, url):  # contact page — reuse the same fixture
            return (url, HTML)

    lead = Lead(
        company="Dental Centar Novak",
        domain="dentalnovak.rs",
        source="osm",
        contact_emails=["INFO@dentalnovak.rs"],  # collector's casing wins
        contact_phones=["021/123-45-67"],  # same number, different format
    )
    enriched = OnePageEnricher(CONFIG).enrich(lead)

    assert enriched is not lead  # new Lead, no in-place mutation
    assert enriched.contact_emails == [
        "INFO@dentalnovak.rs",
        "ordinacija@dentalnovak.rs",
        "marketing@dentalnovak.rs",
    ]
    assert enriched.contact_phones == ["021/123-45-67", "060 555 333 4"]
    assert set(enriched.social_links) == {"facebook", "instagram", "linkedin", "x"}
    # untouched by the contact step
    assert lead.contact_emails == ["INFO@dentalnovak.rs"]


def test_lead_without_domain_passes_through_unchanged():
    lead = Lead(company="No Web Bistro", domain="", source="osm")
    assert ContactEnricher(CONFIG).enrich(lead) is lead
