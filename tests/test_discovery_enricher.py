from pipeline.enrichers.discovery import (
    DiscoveryEnricher,
    candidate_domains,
    proves_identity,
)
from pipeline.schema import Lead

CONFIG = {"enrich": {"http_timeout_s": 5, "lighthouse": {"binary": "lighthouse", "timeout_s": 1}}}


def test_candidate_domains_slug_variants():
    assert candidate_domains("Elemento Detailing") == [
        "elementodetailing.rs",
        "elementodetailing.com",
        "elementodetailing.co.rs",
        "elemento-detailing.rs",
        "elemento-detailing.com",
        "elemento-detailing.co.rs",
    ]


def test_candidate_domains_transliterates_and_handles_empty():
    assert candidate_domains("Đurđević Auto")[0] == "djurdjevicauto.rs"
    assert candidate_domains("!!!") == []


def test_identity_by_phone_in_any_format():
    html = "<html><body><a href='tel:+381 63 821 7839'>zovi</a></body></html>"
    assert proves_identity(html, "Whatever", ["063/821-7839"])


def test_identity_by_company_name_in_title():
    html = "<html><head><title>Elemento Detailing | Beograd</title></head></html>"
    assert proves_identity(html, "Elemento Detailing", [])


def test_unrelated_page_rejected():
    html = "<html><head><title>Domain for sale</title></head><body>Elemento</body></html>"
    assert not proves_identity(html, "Elemento Detailing", ["063 821 7839"])


def _enricher(pages: dict[str, str]):
    enricher = DiscoveryEnricher(CONFIG)

    def fake(domain):
        if domain in pages:
            return f"https://{domain}/", pages[domain]
        raise RuntimeError("homepage unreachable")

    enricher._fetcher._fetch_homepage = fake
    return enricher


def test_discovers_verified_site():
    enricher = _enricher(
        {"elementodetailing.com": "<title>Elemento Detailing</title>"}
    )
    lead = Lead(company="Elemento Detailing", domain="", source="maps")
    assert enricher.enrich(lead).domain == "elementodetailing.com"


def test_unverified_site_not_adopted():
    enricher = _enricher({"elementodetailing.rs": "<title>Parked</title>"})
    lead = Lead(company="Elemento Detailing", domain="", source="maps")
    assert enricher.enrich(lead).domain == ""


def test_lead_with_domain_untouched():
    lead = Lead(company="A", domain="a.rs", source="maps")
    assert _enricher({}).enrich(lead) is lead
