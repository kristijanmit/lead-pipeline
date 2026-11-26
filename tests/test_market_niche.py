"""v2 step 1: market presets, niche definitions, and the config keys they replace."""

import pytest
import yaml

from pipeline.config import ConfigError, load_config
from pipeline.market import Market, load_market, market_yaml
from pipeline.niche import Niche, check_sources, load_niche_file
from pipeline.schema import SCHEMA_VERSION, Lead
from support import MARKETS_YAML, NICHE


def _config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return load_config(path)


def test_presets_load_and_resolve(tmp_path):
    config = _config(tmp_path, MARKETS_YAML)
    uae = load_market(config, "UAE")
    assert (uae.id, uae.country, uae.language, uae.first_touch_excluded) == (
        "uae", "United Arab Emirates", "en", ["WhatsApp"])
    with pytest.raises(ValueError, match="unknown market 'croatia'"):
        load_market(config, "croatia")


def test_repo_config_has_serbia_and_uae():
    config = load_config("config.yaml")
    assert {"serbia", "uae"} <= set(config["markets"])


@pytest.mark.parametrize("change, message", [
    ({"language": "de"}, "language must be one of"),
    ({"channels": ["Instagram", "Fax"]}, "unknown channels"),
    ({"first_touch_excluded": ["WhatsApp"]}, "must also be in channels"),
    ({"dial_code": "381"}, "must start with +"),
    ({"colour": "red"}, "unknown keys"),
])
def test_bad_market_preset_fails_config_load(tmp_path, change, message):
    preset = {"country": "Serbia", "dial_code": "+381", "mobile_prefixes": ["+3816"],
              "language": "sr", "channels": ["Instagram", "Email"], **change}
    with pytest.raises(ConfigError, match=message):
        _config(tmp_path, yaml.safe_dump({"markets": {"serbia": preset}}))


def test_market_missing_a_required_key_fails(tmp_path):
    with pytest.raises(ConfigError, match="missing mobile_prefixes"):
        _config(tmp_path, "markets:\n  serbia: {country: Serbia, dial_code: '+381', language: sr, channels: [Email]}\n")


@pytest.mark.parametrize("text, key", [
    ("collect:\n  categories: [dentist]\n", "collect.categories"),
    ("collect:\n  country: Serbia\n", "collect.country"),
    ("qualification:\n  target_countries: [Serbia]\n", "qualification"),
    ("outreach:\n  industry_labels: {dentist: x}\n", "industry_labels"),
])
def test_removed_default_keys_fail_with_a_pointer(tmp_path, text, key):
    with pytest.raises(ConfigError, match=f"{key}.* was removed"):
        _config(tmp_path, text)


def test_empty_config_needs_no_niche_or_country(tmp_path):
    config = _config(tmp_path, "")
    assert config["markets"] == {}
    assert "industry_labels" not in config["outreach"]
    assert "categories" not in config["collect"] and "country" not in config["collect"]


def test_new_market_yaml_round_trips():
    market = Market(id="croatia", country="Croatia", dial_code="+385", mobile_prefixes=["+3859"],
                    language="en", channels=["Instagram", "Email"])
    body = yaml.safe_load(market_yaml(market))["markets"]["croatia"]
    assert Market.from_dict("croatia", body) == market


def test_niche_requires_name_search_terms_and_phrase():
    with pytest.raises(ValueError, match="missing search_terms, phrase"):
        Niche.from_dict({"name": "dental clinic"})
    with pytest.raises(ValueError, match="missing name"):
        Niche.from_dict({"name": "  ", "search_terms": ["x"], "phrase": "y"})
    with pytest.raises(ValueError, match="unknown keys"):
        Niche.from_dict({**NICHE, "industry_fit": 1})


def test_niche_validates_osm_tags_up_front():
    assert Niche.from_dict({**NICHE, "osm_tags": ["craft=roofer", "dentist"]}).osm_tags == ["craft=roofer", "dentist"]
    with pytest.raises(ValueError, match="unknown category 'dental clinic'"):
        Niche.from_dict({**NICHE, "osm_tags": ["dental clinic"]})


def test_osm_source_needs_osm_tags():
    niche = Niche.from_dict({k: v for k, v in NICHE.items() if k != "osm_tags"})
    check_sources(niche, ["maps", "manual"])
    with pytest.raises(ValueError, match="no osm_tags"):
        check_sources(niche, ["osm", "maps"])


def test_niche_file_accepts_plain_or_wrapped_block(tmp_path):
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(NICHE, allow_unicode=True))
    (tmp_path / "b.yaml").write_text(yaml.safe_dump({"niche": {**NICHE, "search_terms": "dentist"}}))
    assert load_niche_file(tmp_path / "a.yaml") == Niche.from_dict(NICHE)
    assert load_niche_file(tmp_path / "b.yaml").search_terms == ["dentist"]
    with pytest.raises(ValueError, match="not found"):
        load_niche_file(tmp_path / "missing.yaml")


def test_v9_jsonl_loads_with_empty_market_fields():
    assert SCHEMA_VERSION >= 10
    lead = Lead.from_dict({"company": "A", "domain": "a.rs", "source": "osm", "schema_version": 9})
    assert (lead.market, lead.language, lead.niche, lead.niche_label) == ("", "", "", "")
