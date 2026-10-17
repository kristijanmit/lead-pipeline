from pipeline.drafting.channel import is_placeholder_phone, suggest_channel
from pipeline.schema import Lead


def _lead(**kw):
    return Lead(company="X", domain="x.rs", source="osm", **kw)


def test_priority_instagram_then_facebook_then_mobile_then_email_then_call():
    s = {"instagram": "i", "facebook": "f"}
    assert suggest_channel(_lead(social_links=s, contact_phones=["069 111 2233"])) == "Instagram"
    assert suggest_channel(_lead(social_links={"facebook": "f"}, contact_phones=["069 111 2233"])) == "Facebook"
    assert suggest_channel(_lead(contact_phones=["011 3149178", "+381641108811"])) == "Viber/Whatsapp"
    assert suggest_channel(_lead(contact_emails=["a@b.rs"], contact_phones=["011 3149178"])) == "Email"
    assert suggest_channel(_lead(contact_phones=["011 3149178"])) == "Call"
    assert suggest_channel(_lead()) == ""


def test_placeholder_contact_data_is_ignored():
    assert is_placeholder_phone("8001234567") and is_placeholder_phone("+381 11 000 0000")
    assert not is_placeholder_phone("069 706656")
    assert suggest_channel(_lead(contact_phones=["+381 11 000 0000"])) == ""
    assert suggest_channel(_lead(contact_emails=["test@example.com"])) == ""
