import pytest

from pipeline.dedupe import dedupe_leads, normalize_domain
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
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def _lead(company: str, domain: str) -> Lead:
    return Lead(company=company, domain=domain, source="osm")


def test_dedupe_by_domain_first_wins():
    first = _lead("Branch One", "example.com")
    result = dedupe_leads([first, _lead("Branch Two", "example.com")])
    assert result == [first]


def test_distinct_domains_kept():
    leads = [_lead("A", "a.com"), _lead("B", "b.com")]
    assert dedupe_leads(leads) == leads


def test_no_domain_leads_kept_but_deduped_by_company():
    leads = [
        _lead("No Web Bistro", ""),
        _lead("no web bistro ", ""),
        _lead("Other Shop", ""),
    ]
    result = dedupe_leads(leads)
    assert [lead.company for lead in result] == ["No Web Bistro", "Other Shop"]
