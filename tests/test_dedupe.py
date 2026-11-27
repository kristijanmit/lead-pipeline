import pytest

from pipeline.dedupe import dedupe_leads, email_keys, normalize_domain
from pipeline.schema import Lead


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Example.com/", "example.com"),
        ("http://example.com/about/", "example.com"),
        ("www.example.com", "example.com"),
        ("EXAMPLE.COM", "example.com"),
        ("example.com/", "example.com"),
        ("https://example.com:443/shop", "example.com"),
        ("https://sub.example.co.uk/store?x=1", "sub.example.co.uk"),
        ("example.com.", "example.com"),
        ("  https://example.com  ", "example.com"),
        ("", ""),
        (None, ""),
        # shared path hosts: the site's path is part of the key
        ("https://sites.google.com/view/dezurnisto", "sites.google.com/view/dezurnisto"),
        ("https://sites.google.com/view/DezurniSto/kontakt?usp=sharing", "sites.google.com/view/dezurnisto"),
        ("sites.google.com/site/staraordinacija/", "sites.google.com/site/staraordinacija"),
        ("https://www.sites.google.com/view/x", "sites.google.com/view/x"),
        ("https://sites.google.com/", "sites.google.com"),  # no site named
        ("https://sites.google.com/a/clinic.rs/home", "sites.google.com"),  # prefix not listed
        ("https://example.com/view/abc", "example.com"),  # only listed hosts keep a path
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def _lead(company: str, domain: str) -> Lead:
    return Lead(company=company, domain=domain, source="osm")


def test_dedupe_by_domain_first_wins():
    first = _lead("Branch One", "example.com")
    result = dedupe_leads([first, _lead("Branch Two", "example.com")])
    assert [lead.company for lead in result] == ["Branch One"]


def test_distinct_domains_kept():
    leads = [_lead("A", "a.com"), _lead("B", "b.com")]
    result = dedupe_leads(leads)
    assert [lead.company for lead in result] == ["A", "B"]
    assert [lead.listing_count for lead in result] == [1, 1]


def _listing(source: str, address: str = "", url: str = "", domain: str = "chain.rs") -> Lead:
    return Lead(company="X", domain=domain, source=source, address=address, profile_url=url)


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # one business, listed once by each source: not a chain
        ([_listing("osm", "Main 1"), _listing("maps", "Main 1, Novi Sad", "cid=1")], 1),
        # neighbouring grid cells returning the same place
        ([_listing("maps", "Main 1", "cid=1"), _listing("maps", " main  1", "cid=1"),
          _listing("maps", "Main 1", "cid=1")], 1),
        # three branches on one domain
        ([_listing("maps", "Main 1"), _listing("maps", "Side 2"), _listing("maps", "Park 3"),
          _listing("osm", "Main 1")], 3),
        # no address: the listing page tells them apart
        ([_listing("maps", url="cid=1"), _listing("maps", url="cid=2")], 2),
        # neither: can't be told apart, counts once
        ([_listing("manual"), _listing("manual")], 1),
        # highest per source, not the sum
        ([_listing("osm", "A"), _listing("osm", "B"), _listing("maps", "A x"), _listing("maps", "B x")], 2),
    ],
)
def test_listing_count(rows, expected):
    [lead] = dedupe_leads(rows)
    assert lead.listing_count == expected


def test_listing_count_for_company_keyed_leads():
    rows = [_listing("maps", "A", domain=""), _listing("maps", "B", domain="")]
    [lead] = dedupe_leads(rows)
    assert lead.listing_count == 2


def test_no_domain_leads_kept_but_deduped_by_company():
    leads = [
        _lead("No Web Bistro", ""),
        _lead("no web bistro ", ""),
        _lead("Other Shop", ""),
    ]
    result = dedupe_leads(leads)
    assert [lead.company for lead in result] == ["No Web Bistro", "Other Shop"]


def test_email_keys_lowercased_and_blank_dropped():
    lead = Lead(
        company="Mirković",
        domain="",
        source="osm",
        contact_emails=["Info@DentalCentarMirkovic.com", "  "],
    )
    assert email_keys(lead) == ["info@dentalcentarmirkovic.com"]


def test_email_keys_empty_when_no_emails():
    assert email_keys(_lead("No Web Bistro", "")) == []


@pytest.mark.parametrize(
    "raw, domain, socials",
    [
        ("https://www.facebook.com/shineon", "", {"facebook": "https://www.facebook.com/shineon"}),
        ("m.facebook.com/shineon", "", {"facebook": "https://m.facebook.com/shineon"}),
        ("instagram.com/shineon", "", {"instagram": "https://instagram.com/shineon"}),
        ("https://twitter.com/shineon", "", {"x": "https://twitter.com/shineon"}),
        ("https://www.example.com/", "example.com", {}),
        ("", "", {}),
        (None, "", {}),
    ],
)
def test_split_website(raw, domain, socials):
    from pipeline.dedupe import split_website

    assert split_website(raw) == (domain, socials)


def test_duplicate_fills_missing_fields_without_overwriting():
    osm = Lead(
        company="Dental Novak", domain="dentalnovak.rs", source="osm",
        address="Bulevar 1, Novi Sad", contact_phones=["021/452-333"],
        profile_url="https://www.openstreetmap.org/node/1",
    )
    maps = Lead(
        company="Dental Centar Novak", domain="dentalnovak.rs", source="maps",
        address="Other address", rating=4.9, review_count=127,
        contact_emails=["info@dentalnovak.rs"],
        contact_phones=["+381 21 452 333", "+381 64 111 222"],
        profile_url="https://maps.google.com/?cid=101",
    )
    (merged,) = dedupe_leads([osm, maps])
    assert merged.company == "Dental Novak" and merged.source == "osm"
    assert merged.address == "Bulevar 1, Novi Sad"  # kept's value wins
    assert (merged.rating, merged.review_count) == (4.9, 127)
    assert merged.contact_emails == ["info@dentalnovak.rs"]
    # same number in two spellings is not repeated; the new one is added
    assert merged.contact_phones == ["021/452-333", "+381 64 111 222"]
    assert merged.profile_url == "https://maps.google.com/?cid=101"  # Maps beats OSM


def test_two_google_sites_leads_stay_apart():
    leads = [
        Lead(company="A", domain=normalize_domain("https://sites.google.com/view/a"), source="maps"),
        Lead(company="B", domain=normalize_domain("https://sites.google.com/view/b"), source="maps"),
    ]
    result = dedupe_leads(leads)
    assert [(lead.company, lead.listing_count) for lead in result] == [("A", 1), ("B", 1)]
