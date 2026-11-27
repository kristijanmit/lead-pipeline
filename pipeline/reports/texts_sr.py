"""Serbian audit report wording (Latin script, polite "Vi" form, capitalized).

Written for the business owner: what a visitor sees and why it matters. No tool names,
scores, ratings or "besplatno" (the same rules as the first message, docs/OUTREACH.md)."""

LANG = "sr"
TITLE = "Provera sajta: {company}"
CHECKED = "{domain} · provereno {date}"


def date(d) -> str:
    return f"{d.day}. {d.month}. {d.year}."


INTRO = "Šta vredi popraviti na sajtu {domain}:"

# item -> (title, text). Placeholders: {domain}, {year}, {cms}
FINDINGS = {
    "cert_error": (
        "Pregledač prikazuje bezbednosno upozorenje",
        "Kada se otvori {domain}, pre stranice se pojavi upozorenje da veza nije bezbedna, pa se većina "
        "posetilaca vrati nazad. Rešava se obnavljanjem bezbednosnog sertifikata sajta.",
    ),
    "not_mobile": (
        "Teško se koristi na telefonu",
        "Sajt se na telefonu otvara u verziji za računar, pa posetioci moraju da zumiraju i pomeraju stranicu "
        "u stranu. Većina ljudi koji Vas traže to radi preko telefona.",
    ),
    "no_https": (
        "Pregledač ga označava kao „nije bezbedno“",
        "Sajt se ne otvara preko bezbedne veze (https), pa ga Chrome i Safari pored adrese označavaju kao "
        "„nije bezbedno“. Posetioci to primete, a Google takve sajtove rangira niže.",
    ),
    "outdated": (
        "Deluje zastarelo",
        "U dnu sajta i dalje piše © {year}. Posetiocu to deluje kao da se sajt ne održava, ili da firma možda "
        "više ne radi.",
    ),
    "outdated_cms": (
        "Napravljen na starijoj platformi",
        "Sajt radi na platformi {cms}, koju danas retko ko koristi za nove sajtove. Teže se održava i menja, "
        "i obično deluje zastarelo pored novijih sajtova.",
    ),
    "perf_below_50": (
        "Sporo se učitava na telefonu",
        "Na običnoj mobilnoj vezi početnoj stranici treba dosta vremena da se pojavi. Mnogi posetioci odu pre "
        "nego što se učita, a Google uzima brzinu u obzir.",
    ),
    "no_booking_or_call": (
        "Nema brzog načina da Vas kontaktiraju",
        "Nema dugmeta za poziv, forme za zakazivanje ni Viber/WhatsApp dugmeta, pa posetilac na telefonu mora "
        "da prepisuje broj ili traži drugi način da Vas kontaktira.",
    ),
    "seo_below_70": (
        "Google ga lako previdi",
        "Nedostaju neke osnovne stvari koje Google čita da bi razumeo stranicu, pa se sajt ređe pojavljuje "
        "kada ljudi traže ono čime se bavite.",
    ),
}

WORKS_HEADING = "Šta već dobro radi"
WORKS = {
    "https": "Bezbedna veza (https)",
    "mobile": "Prilagođen ekranu telefona",
    "fast": "Brzo se učitava",
    "click_to_call": "Broj telefona koji se poziva jednim dodirom",
    "booking": "Online zakazivanje",
    "messaging": "Viber / WhatsApp dugme",
    "recent": "Redovno se ažurira",
}

CLOSING = "Rado ću Vam objasniti bilo koju od ovih stavki."
