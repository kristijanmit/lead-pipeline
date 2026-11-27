"""profile_facts (script_edits.md §11.3): pure, from homepage HTML + Maps fields."""

from pathlib import Path

from pipeline.enrichers.profile import MAX_CHARS, MAX_FACTS, build_profile_facts
from pipeline.schema import Lead

FIXTURES = Path(__file__).parent / "fixtures"


def _lead(**kw):
    return Lead(company=kw.pop("company", "Zubić Dent"), domain="zubic.rs", source="maps", **kw)


_HTML = """<html><head><title>Zubić Dent</title>
<meta name="description" content="Stomatološka ordinacija u Novom Sadu, implantologija i estetska stomatologija.">
</head><body>
<h1>Početna</h1><h2>O nama</h2><h2>Usluge</h2><h2>Implantologija</h2><h3>Kontakt</h3>
<h2>Ova stranica koristi kolačiće</h2><h3>Implantologija</h3><h3>Contact us</h3><h2>Menu</h2>
<h2>Estetska stomatologija i izbeljivanje zuba</h2><h3>Dečja stomatologija</h3><h3>Ortodoncija</h3>
</body></html>"""


def test_order_filters_dedupes_and_caps():
    facts = build_profile_facts(
        _lead(maps_category="Dentist", maps_description="Porodična ordinacija od 1998."), _HTML
    )
    assert facts[0] == "Google Maps description: Porodična ordinacija od 1998."
    assert facts[1].startswith("Website description: Stomatološka ordinacija u Novom Sadu")
    assert facts[2] == "Named on the website: Implantologija"
    assert len(facts) == MAX_FACTS
    assert all(len(f) <= MAX_CHARS for f in facts)
    joined = " ".join(facts).lower()
    for generic in ("početna", "o nama", "usluge", "kontakt", "kolačić", "contact us", "menu"):
        assert f": {generic}" not in joined
    assert sum("Implantologija" == f.split(": ", 1)[1] for f in facts) == 1  # deduped
    assert not any(f.startswith("Website title") for f in facts)  # title is just the company name


def test_long_fact_is_clipped_on_a_word():
    desc = "Ordinacija " + "veoma " * 30 + "kraj"
    fact = build_profile_facts(_lead(maps_description=desc))[0]
    assert len(fact) <= MAX_CHARS and fact.endswith("…") and "  " not in fact


def test_maps_only_lead_without_html():
    assert build_profile_facts(_lead(maps_category="Dental clinic")) == ["Google Maps category: Dental clinic"]
    assert build_profile_facts(_lead()) == []


def test_fixture_pages_give_facts_without_boilerplate():
    for name in ("modern_site.html", "old_site.html"):
        facts = build_profile_facts(_lead(company="x"), (FIXTURES / name).read_text(encoding="utf-8"))
        assert len(facts) <= MAX_FACTS
        assert all(": " in f and len(f) <= MAX_CHARS for f in facts)


def test_generic_first_part_and_pitch_openers_are_dropped():
    html = ("<h2>O nama – Dental Spa Šabac</h2><h2>Contact | Smile</h2><h2>Zašto odabrati Dental Spa</h2>"
            "<h2>Welcome to Smile Clinic</h2><h2>Endodoncija – lečenje kanala</h2>")
    assert build_profile_facts(_lead(company="x"), html) == ["Named on the website: Endodoncija – lečenje kanala"]
