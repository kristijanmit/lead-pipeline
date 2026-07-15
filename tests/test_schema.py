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


def test_defaults():
    lead = Lead(company="A", domain="a.com", source="osm")
    assert lead.status == "collected"
    assert lead.contact_emails == []
    assert lead.total_score is None
