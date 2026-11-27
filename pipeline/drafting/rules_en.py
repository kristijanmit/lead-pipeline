"""English drafting rule set (`language: en`, e.g. the UAE market) — mirrors the
Serbian rules (rules_sr.py) with English wording, and config/outreach.en.md as the
voice references. WhatsApp is never a first touch in the UAE, so there is no
WhatsApp format here.
"""

from __future__ import annotations

from pipeline.drafting.rules import Rules
from pipeline.schema import Lead

SYSTEM = (
    "You write short first-contact outreach messages in English for a small web studio. "
    "Output only the requested fields."
)

PLAIN_PRAISE = "I like the way you present your services"
SENDER = "I build websites for local businesses"
SENDER_CLINIC = "I build websites for clinics"

# (label, regex), case-insensitive. Union of script_edits.md §9.2 and §10.1.
BANNED = (
    ("free", r"\bfree\b"), ("no obligation", r"\bno obligation"), ("this week", r"\bthis week\b"),
    ("lighthouse", r"\blighthouse\b"), ("SEO", r"\bseo\b"), ("AI", r"\bai\b"),
    ("automation", r"\bautomat(?:ion|ed|e)\b"),
    ("couldn't find", r"\bcould(?:n'?t| not) find"),
    ("don't have a website", r"\b(?:don'?t|do not) have (?:a )?(?:web)?site"),
    ("rating", r"\brating"), ("review", r"\breview"), ("stars", r"\bstars?\b"),
    ("Dear Sir", r"\bdear sir"), ("hope this finds you", r"\bhope this finds you"),
    ("hope you're well", r"\bhope you(?:'re| are) well"),
)


def prompt_rules(
    lead: Lead, channel: str, outreach: dict, niche: str, question_rule: str, offer: str,
    review_strong: int | None = None,  # unused: no English format cites reviews
) -> list[str]:
    place = "page" if channel == "Facebook" else "profile"
    audit = offer == "audit"
    closing = (
        "[one offer phrased as a question]?\" 2-3 sentences, the offer is always last"
        if audit else "[one question]?\" 2-3 sentences, the question is always last, and only ONE easy question"
    )
    rules = [
        f'Structure for a DM: "Hi, I came across your {place} and [concrete praise]. I\'m Kristijan, I build '
        'websites for local businesses, so I\'m curious ' + closing + ".",
        'Greeting: "Hi" for a DM, "Hello" for Email and Call. Never "Dear Sir/Madam", "Hope you\'re well" or '
        '"I hope this finds you well". Personal voice ("I\'m Kristijan"), never "we at ONIX". Sender sentence, '
        f'word for word: "I\'m Kristijan, {SENDER}" (or "{SENDER_CLINIC}" when the niche is a clinic). Never '
        'add services to it (no "and booking systems", "and automation" or anything else).',
        "Tone: natural, spoken, warm, modest, curious. Not an ad, not a template. The question sounds like "
        "genuine interest. No emoji, no exclamation marks, no superlatives, no sales phrases, no price, "
        "no offer of services" + (" other than the one audit offer." if audit else ".")
        + ' Never use the words "free", "AI", "automation" or "SEO".',
        "Plain international English, Latin script only (even if the business name is in Arabic script: "
        "use its English or transliterated name).",
        "Praise only what the operator's note or the bio says (photos, videos, before/after, a concrete "
        "treatment or service, a slogan). The operator's own wording sets the strength. If there is no note and "
        f'nothing concrete, use the plain "{PLAIN_PRAISE}" and put "a concrete detail would be better" in '
        "verify_note. Never invent details.",
        "Never mention the Google rating, reviews or stars. Never write that you could not find their site or "
        "that they have none; a gap in the website is surfaced only through the question. Never criticize the "
        "design, speed or age of an existing site. Never mention Lighthouse numbers or audit scores. Never "
        "mention client names or any patient-records system.",
        "Exception, friendly warning: ONLY if the operator's note or the audit notes state a concrete, checkable "
        "problem (bio link opens an error page, outdated phone number, not mobile-friendly), state it plainly as "
        "a heads-up before the question, no judgement, and tell the sender in verify_note to check it.",
        question_rule,
    ]
    if channel == "Email":
        rules.append(
            'Format: first line "Subject: <short, specific>", blank line, then "Hello, this is Kristijan, I build '
            'websites for local businesses..." with the one praise point and the question, body of 70-110 words, '
            'a one-line opt-out ("If this isn\'t relevant, just let me know and I won\'t write again."), then the '
            f'signature "{outreach["signature"]}" as the last line.'
        )
    elif channel == "Call":
        rules.append(
            'Format: a short call script: "Hello, this is Kristijan, I build websites for local businesses. '
            'I came across [business], do you have a minute for one quick question about how new clients find you?"'
        )
    elif channel == "Facebook":
        rules.append(
            "Facebook posts reach a page admin: after the question add a short line asking to pass the message "
            'to the owner if someone else reads it ("If someone from the team reads this, please pass it on to '
            'the owner.").'
        )
    elif channel == "LinkedIn":
        rules.append(
            "LinkedIn, after the connection is accepted: start with \"Hi, thanks for connecting.\", then the DM "
            "structure (who you are, one concrete point, one question last)."
        )
    return rules


RULES = Rules(
    language="en",
    system=SYSTEM,
    banned=BANNED,
    channel_allowed={},
    script_re=r"[Ѐ-ӿ؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]",
    script_problem="contains Cyrillic or Arabic script; use Latin script only",
    greetings={"": "Hi", "Email": "Hello", "Call": "Hello"},
    max_questions=1,
    no_links=True,
    sender_lines={"": (SENDER, SENDER_CLINIC)},
    prompt_rules=prompt_rules,
    connection_note_rule=(
        'Connection note: one or two sentences, max 300 characters, starts with "Hi", says where you came '
        "across the business and that you build websites for local businesses (or clinics), would be glad to "
        "connect. No pitch, no question needed. You don't know the person's name: never write a placeholder."
    ),
    plain_praise=PLAIN_PRAISE,
)
