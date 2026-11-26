import pytest
import yaml

from pipeline.channels import best_channel, is_mobile, normalize_phone, usable_channels
from pipeline.drafting.channel import is_placeholder_phone, suggest_channel
from pipeline.market import Market
from pipeline.schema import Lead
from support import MARKETS_YAML

_PRESETS = yaml.safe_load(MARKETS_YAML)["markets"]
SERBIA = Market.from_dict("serbia", _PRESETS["serbia"])
UAE = Market.from_dict("uae", _PRESETS["uae"])


def _lead(**kw):
    return Lead(company="X", domain="x.rs", source="osm", **kw)


def test_serbia_priority_instagram_then_facebook_then_mobile_then_email_then_call():
    s = {"instagram": "i", "facebook": "f"}
    assert suggest_channel(_lead(social_links=s, contact_phones=["069 111 2233"]), SERBIA) == "Instagram"
    assert suggest_channel(_lead(social_links={"facebook": "f"}, contact_phones=["069 111 2233"]), SERBIA) == "Facebook"
    assert suggest_channel(_lead(contact_phones=["011 3149178", "+381641108811"]), SERBIA) == "Viber/Whatsapp"
    assert suggest_channel(_lead(contact_emails=["a@b.rs"], contact_phones=["011 3149178"]), SERBIA) == "Email"
    assert suggest_channel(_lead(contact_phones=["011 3149178"]), SERBIA) == "Call"
    assert suggest_channel(_lead(), SERBIA) == ""


def test_placeholder_contact_data_is_ignored():
    assert is_placeholder_phone("8001234567") and is_placeholder_phone("+381 11 000 0000")
    assert not is_placeholder_phone("069 706656")
    assert suggest_channel(_lead(contact_phones=["+381 11 000 0000"]), SERBIA) == ""
    assert suggest_channel(_lead(contact_emails=["test@example.com"]), SERBIA) == ""
    # the old is_serbian_mobile special-cased 060000…; the placeholder filter covers it
    assert suggest_channel(_lead(contact_phones=["060 000 0000"]), SERBIA) == ""


@pytest.mark.parametrize("phone, market, expected", [
    ("050 482 7391", UAE, "+971504827391"),
    ("00971 50 482 7391", UAE, "+971504827391"),
    ("+971 50 482 7391", SERBIA, "+971504827391"),  # an explicit country code wins
    ("069 111 2233", SERBIA, "+381691112233"),
    ("381 64 110 8811", SERBIA, "+381641108811"),
    ("", SERBIA, ""),
])
def test_normalize_phone(phone, market, expected):
    assert normalize_phone(phone, market) == expected


def test_mobile_detection_follows_the_market():
    assert is_mobile("+971 50 482 7391", UAE) and not is_mobile("+971 50 482 7391", SERBIA)
    assert is_mobile("050 482 7391", UAE) and not is_mobile("050 482 7391", SERBIA)
    assert is_mobile("069 111 2233", SERBIA) and not is_mobile("069 111 2233", UAE)
    assert not is_mobile("04 482 7391", UAE)  # Dubai landline


def test_uae_never_suggests_whatsapp_for_first_touch_but_lists_it():
    mobile_only = _lead(contact_phones=["050 482 7391"])
    assert usable_channels(mobile_only, UAE) == ["Call", "WhatsApp"]
    assert best_channel(mobile_only, UAE) == "Call"
    with_email = _lead(contact_phones=["050 482 7391"], contact_emails=["info@clinic.ae"])
    assert best_channel(with_email, UAE) == "Email"


def test_uae_order_puts_linkedin_before_email():
    lead = _lead(social_links={"linkedin": "l"}, contact_emails=["info@clinic.ae"])
    assert usable_channels(lead, UAE) == ["LinkedIn", "Email"]
    assert best_channel(lead, UAE) == "LinkedIn"
    assert best_channel(_lead(social_links={"linkedin": "l", "facebook": "f"}), UAE) == "Facebook"


def test_serbia_never_uses_linkedin():
    lead = _lead(social_links={"linkedin": "l"}, contact_emails=["a@b.rs"])
    assert usable_channels(lead, SERBIA) == ["Email"]
    assert best_channel(_lead(social_links={"linkedin": "l"}), SERBIA) == ""


def test_label_is_the_markets_own():
    assert usable_channels(_lead(contact_phones=["+381641108811"]), SERBIA) == ["Viber/Whatsapp", "Call"]
    assert "Viber/Whatsapp" not in usable_channels(_lead(contact_phones=["050 482 7391"]), UAE)
