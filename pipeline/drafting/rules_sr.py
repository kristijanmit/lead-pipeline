"""Serbian drafting rule set (`language: sr`) — the operator's ONIX outreach rules
(docs/OUTREACH.md), moved here unchanged from drafter.py in v2 step 7.

Viber/Whatsapp has its own format (found on Google Maps, offers a free example,
may cite reviews); it applies only where the market's mobile channel is labelled
"Viber/Whatsapp" (Serbia).
"""

from __future__ import annotations

from pipeline.drafting.rules import Rules
from pipeline.schema import Lead

SYSTEM = (
    "You write short first-contact outreach messages in Serbian for a small web studio. "
    "Output only the requested fields."
)

PLAIN_PRAISE = "sviđa mi se kako radite"

# (label, regex): substrings as before step 7 ("zvezdic", "recenzij" are stems)
BANNED = (
    ("bez obaveze", r"bez obaveze"), ("besplatno", r"besplatno"), ("ove nedelje", r"ove nedelje"),
    ("lighthouse", r"lighthouse"), ("seo", r"seo "), ("nisam uspeo", r"nisam uspeo"),
    ("nisam pronašao", r"nisam pronašao"), ("nemate sajt", r"nemate sajt"),
    ("unapređujem", r"unapređujem"), ("zvezdic", r"zvezdic"), ("recenzij", r"recenzij"),
)
VIBER = "Viber/Whatsapp"
SENDER = "pravim sajtove za lokalne firme"
SENDER_VIBER = "pravim sajtove za lokalne biznise"
_SENDER_RULE = (
    'Sender sentence, word for word: "{line}". Never add services to it (no "online zakazivanje", '
    '"i održavanje", "za ordinacije" or anything else).'
)


def _reviews_rule(lead: Lead, review_strong: int | None) -> str:
    """A review claim ("dosta ocena") only when the lead has at least the market's
    review_strong reviews; the validator enforces the same threshold."""
    if review_strong is not None and (lead.review_count or 0) >= review_strong:
        return (
            f" Google data for this business: rating {lead.rating}, {lead.review_count} reviews. "
            'You may mention it only as "dosta ocena"; never give numbers.'
        )
    return (
        ' Do NOT mention Google ratings or reviews at all (no "dosta ocena", no "dobre ocene"): there are too few '
        "to cite. Use photos or work from the operator's note or the bio for the observation, or leave it out."
    )

def _viber_redesign_rules(lead: Lead, reviews: str) -> list[str]:
    """Viber/WhatsApp for a lead that already has a site: found while searching
    their city, pitches a new look instead of a first site."""
    return [
        f'Format (follow the VOICE REFERENCES closely): "Pozdrav, naišao sam na vaš sajt dok sam tražio '
        f'[servise/radionice/studije...] u {lead.location or "[grad]"}. Ja sam Kristijan, pravim sajtove za '
        'lokalne biznise." then a paragraph: "Vidim da imate [one true observation], pa me zanima da li ste '
        'razmišljali o novom izgledu sajta." plus the benefit (clients can book / message / ask for an offer '
        'straight from the site while they work), then the line "Da li želite da vam pokažem besplatan primer '
        'kako bi izgledao novi sajt?", then the niche link line.',
        'Greeting "Pozdrav". Casing and the polite form follow the references (lowercase "vaš", "vam"). '
        "Latin script with diacritics. No emoji, no exclamation marks, no price, no superlatives.",
        "The observation must be true: \'lepe slike radova\' or \'dobre ocene\' only if the operator's note, the "
        "bio or the data below says so." + reviews,
        'Decline the city into the locative case after "u" ("u Beogradu", "u Nišu", "u Zemunu"), never the '
        "nominative. If unsure of the form, rephrase so the city is not needed.",
        "Never criticize their existing site (design, speed, age) and never mention Lighthouse numbers; only "
        "say a new look could help.",
        "Link: end with the niche link line only if its URL appears verbatim under a matching niche in the "
        "references; copy that URL exactly (no https). If no reference matches the trade, leave the link line "
        "out. Never invent a URL.",
        "Adapt the business noun and the benefit to the trade. Online booking only makes sense for trades that "
        "take appointments.",
    ]


def _viber_rules(lead: Lead, niche: str, review_strong: int | None = None) -> list[str]:
    """Viber/WhatsApp is a different format from the Instagram DM: it says where
    the number was found (Google Maps), names the benefit, offers a free example of
    their site and links the niche page. The Instagram rules forbid exactly these."""
    reviews = _reviews_rule(lead, review_strong)
    sender = _SENDER_RULE.format(line=SENDER_VIBER)
    if lead.domain:
        return _viber_redesign_rules(lead, reviews) + [sender]
    return [sender, 
        'Format (follow the VOICE REFERENCES closely): "Pozdrav, naišao sam na [vašu firmu/radionicu/servis...] '
        'na Google mapama. Ja sam Kristijan, pravim sajtove za lokalne biznise." then a paragraph with one '
        "concrete observation, the question whether they have thought about a website and the benefit "
        "(better Google position, more clients, clients can book / message / ask for an offer while they work), "
        'then the line "Da li želite da vam pošaljem primer vašeg sajta besplatno?", then the niche link line.',
        'Greeting "Pozdrav". Casing and the polite form follow the references (lowercase "vaš", "vam"). '
        "Latin script with diacritics. No emoji, no exclamation marks, no price, no superlatives.",
        "The observation must be true: use the operator's note or the bio for praise (\'dobre slike radova\'). "
        "If there is nothing concrete, use the plain wording from the references." + reviews,
        "Never write that you could not find their site or that they have none, and never criticize an "
        "existing site; only ask whether they have thought about a website. Never mention Lighthouse numbers.",
        "Adapt the business noun and the benefit to the trade (a workshop, a service, a studio, a shop). "
        "Online booking only makes sense for trades that take appointments.",
        "Link: end with the niche link line only if its URL appears verbatim under a matching niche in the "
        "references; copy that URL exactly. If no reference matches the trade, leave the link line out. "
        "Never invent a URL.",
    ]



def prompt_rules(
    lead: Lead, channel: str, outreach: dict, niche: str, question_rule: str, offer: str,
    review_strong: int | None = None,
) -> list[str]:
    if channel == VIBER:
        return _viber_rules(lead, niche, review_strong)  # its own format: own closing offer and link line
    is_dm = channel not in ("Email", "Call")
    greeting = "Pozdrav" if is_dm else "Dobar dan, ovde Kristijan"
    place = "stranicu" if channel == "Facebook" else "profil"
    audit = offer == "audit"
    closing = (
        "[one offer phrased as a question]?\" 2-3 sentences, the offer is always last"
        if audit else "[one question]?\" 2-3 sentences, the question is always last, and only ONE easy question"
    )
    rules = [
        "Structure for a DM: \"Pozdrav, naišao sam na Vaš " + place + " i [concrete praise]. Ja sam Kristijan, "
        "pravim sajtove za lokalne firme, pa me zanima " + closing + ".",
        f'Greeting: "{greeting}". ' + _SENDER_RULE.format(line=SENDER) + ' Never "pravim i unapređujem".',
        "Tone: natural, spoken, warm, modest, curious. Not an ad, not a template. The question sounds like "
        "genuine interest. No emoji, no exclamation marks, no superlatives, no sales phrases, no price, "
        "no offer of services" + (" other than the one audit offer." if audit else "."),
        'Serbian, Latin script with diacritics (even if the company name is Cyrillic). Polite "Vi" form, '
        'always capitalized: Vaš, Vam, Vas, Vaši. Avoid "Vam" and "Vas" in the same sentence '
        '("kako Vam novi klijenti najčešće stižu?" or "kako novi klijenti najčešće dolaze do Vas?").',
        "Praise only what the operator's note or the bio says (photos, videos, before/after, experience, a "
        "concrete service, a slogan). The operator's own wording sets the strength: \"dobre slike\" stays "
        f'"dobre". If there is no note and nothing concrete, use the plain "{PLAIN_PRAISE}" and put '
        '"a concrete detail would be better" in verify_note. If the profile looks weak, praise the service '
        "from the bio or leave the praise out. Never invent details.",
        "Never mention the Google rating or review count. Never write that you could not find their site or "
        "that they have none; a gap in the website is surfaced only through the question. Never criticize the "
        "design, speed or age of an existing site. Never mention Lighthouse numbers or audit scores.",
        "Exception, friendly warning: ONLY if the operator's note or the audit notes state a concrete, checkable "
        'problem (bio link dead, site title "My WordPress", not mobile-friendly), state it as a helpful heads-up '
        'before the question ("Primetio sam da link u bio-u ne radi, pa sam hteo da Vas upozorim jer verovatno '
        'gubite klijente koji kliknu."), and tell the sender in verify_note to check it before sending.',
        question_rule,
    ]
    if channel == "Email":
        rules.append(
            'Format: first line "Subject: <short, specific>", blank line, then "Dobar dan, ovde Kristijan, '
            'pravim sajtove za lokalne firme..." with the one praise point and the question, body of 70-110 '
            'words, a one-line opt-out ("Ako Vam ovo nije od interesa, slobodno mi recite i neću Vam više '
            f'pisati."), then the signature "{outreach["signature"]}".'
        )
    elif channel == "Call":
        rules.append(
            'Format: a short call script: "Dobar dan, ovde Kristijan, pravim sajtove za lokalne firme. '
            'Video sam [firma], imate li minut za jedno kratko pitanje o tome kako Vam stižu novi klijenti?"'
        )
    elif channel == "Facebook":
        rules.append('Facebook posts reach a page admin: after the question add a short line asking to pass the message to the owner if someone else reads it.')
    return rules


RULES = Rules(
    language="sr",
    system=SYSTEM,
    banned=BANNED,
    # the Viber format offers a free example and cites Google reviews
    channel_allowed={VIBER: ("besplatno", "recenzij")},
    script_re=r"[Ѐ-ӿ]",
    script_problem="contains Cyrillic; use Latin script",
    greetings={"": "Pozdrav", "Email": "Dobar dan", "Call": "Dobar dan"},
    rating_re=r"\bocen\w*",  # "ocena", "ocene"; not "procena"
    rating_allowed=(VIBER,),
    review_labels=("recenzij",),
    sender_lines={"": (SENDER,), VIBER: (SENDER_VIBER,)},
    prompt_rules=prompt_rules,
    connection_note_rule=(
        'Connection note: one or two sentences, max 300 characters, starts with "Pozdrav", says where you '
        "found the business and that you make websites for local firms, no pitch, no question needed."
    ),
    plain_praise=PLAIN_PRAISE,
)
