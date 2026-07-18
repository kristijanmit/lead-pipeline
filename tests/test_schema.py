from pipeline.schema import SCHEMA_VERSION, Lead, read_jsonl, write_jsonl


def test_jsonl_round_trip(tmp_path):
    leads = [
        Lead(
            company="Bäckerei Schmidt",
            domain="baeckerei-schmidt.de",
            source="osm",
            industry="bakery",
            contact_emails=["info@baeckerei-schmidt.de", "sales@baeckerei-schmidt.de"],
            social_links={"instagram": "https://instagram.com/schmidt"},
            errors=["contact page timed out"],
        ),
        Lead(company="No Web Bistro", domain="", source="osm"),
    ]
    path = tmp_path / "leads.jsonl"
    assert write_jsonl(path, leads) == 2
    assert read_jsonl(path) == leads


def test_from_dict_ignores_unknown_fields():
    lead = Lead.from_dict(
        {
            "company": "A",
            "domain": "a.com",
            "source": "manual",
            "some_future_field": 42,
        }
    )
    assert lead.company == "A"
    assert lead.schema_version == SCHEMA_VERSION


def test_from_dict_migrates_legacy_contact_phone():
    # schema v2 JSONL (the first real runs) stored a single string
    lead = Lead.from_dict(
        {
            "company": "A",
            "domain": "a.com",
            "source": "osm",
            "contact_phone": "+381 21 123 456",
        }
    )
    assert lead.contact_phones == ["+381 21 123 456"]


def test_from_dict_migrates_legacy_website_audit_score():
    # schema v5 and earlier JSONL stored this under the old field name
    lead = Lead.from_dict(
        {
            "company": "A",
            "domain": "a.com",
            "source": "osm",
            "website_audit_score": 72.5,
        }
    )
    assert lead.opportunity_score == 72.5


def test_defaults():
    lead = Lead(company="A", domain="a.com", source="osm")
    assert lead.status == "collected"
    assert lead.contact_emails == []
    assert lead.total_score is None
    assert lead.qualified is False
    assert lead.country == ""
