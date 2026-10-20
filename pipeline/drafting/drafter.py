"""Outreach drafting — one LLM call per lead, via the `claude` CLI.

Drafts are text for a human to review and send; nothing here sends anything.
The CLI call is isolated in _call_claude() so swapping it for an API-key
client later is a one-function change. The call runs with skills, MCP servers,
and settings disabled and from an empty temp dir (no CLAUDE.md pickup) —
without that the CLI loads ~135k tokens of context per call instead of ~1k.

The copy rules below are the operator's ONIX outreach instructions (docs/OUTREACH.md).
Hard rules the model can break are re-checked in code by validate_message();
a violation gets one retry with the problem named, then lands in Lead.errors.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from pipeline.drafting.channel import suggest_channel
from pipeline.schema import Lead

MAX_CHARS = 2000

# phrases the operator never wants, checked case-insensitively
_BANNED = (
    "bez obaveze", "besplatno", "ove nedelje", "lighthouse", "seo ",
    "nisam uspeo", "nemate sajt", "unapređujem", "zvezdic", "recenzij",
)
_RATING_RE = re.compile(r"\bocen\w*", re.IGNORECASE)  # "ocena", "ocene"; not "procena"
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF]")

_SCHEMA = {
    "type": "object",
    "required": ["message", "verify_note"],
    "properties": {
        "message": {"type": "string"},
        "verify_note": {"type": "string"},
    },
}

_SYSTEM = (
    "You write short first-contact outreach messages in Serbian for a small web studio. "
    "Output only the requested fields."
)


def validate_message(message: str, channel: str) -> list[str]:
    """Deterministic re-check of the rules a model can silently break."""
    problems = []
    text = message.strip()
    if not text:
        return ["message is empty"]
    if len(text) >= MAX_CHARS:
        problems.append(f"message is {len(text)} chars, limit is {MAX_CHARS - 1}")
    if re.search(r"[Ѐ-ӿ]", text):
        problems.append("contains Cyrillic; use Latin script")
    if "!" in text:
        problems.append("contains an exclamation mark")
    low = text.lower()
    for phrase in _BANNED:
        if phrase in low:
            problems.append(f"contains banned phrase {phrase.strip()!r}")
    if _RATING_RE.search(text):
        problems.append("mentions a rating; never mention Google ratings or reviews")
    if _EMOJI_RE.search(text):
        problems.append("contains an emoji")
    body = low
    if channel == "Email" and low.startswith("subject:"):
        body = low.partition("\n")[2].strip()
    greeting = "dobar dan" if channel in ("Email", "Call") else "pozdrav"
    if not body.startswith(greeting):
        problems.append(f"must start with {greeting.capitalize()!r}")
    if channel != "Call" and "?" not in text:
        problems.append("must contain one question")
    if channel == "Email" and not low.startswith("subject:"):
        problems.append("email must start with a 'Subject:' line")
    if channel not in ("Email", "Call") and "subject:" in low:
        problems.append("chat messages must not have a subject line")
    return problems


def load_examples(path: str) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def build_prompt(lead: Lead, channel: str, outreach: dict, variant: int, examples: str) -> str:
    # `variant` is unused now (one fixed descriptor and a question chosen by business type);
    # kept so the runner call sites and signature stay stable.
    socials = lead.social_links
    niche = outreach["industry_labels"].get(lead.industry, lead.industry)
    is_dm = channel not in ("Email", "Call")
    greeting = "Pozdrav" if is_dm else "Dobar dan, ovde Kristijan"
    place = "stranicu" if channel == "Facebook" else "profil"

    rules = [
        "Structure for a DM: \"Pozdrav, naišao sam na Vaš " + place + " i [concrete praise]. Ja sam Kristijan, "
        "pravim sajtove za lokalne firme, pa me zanima [one question]?\" 2-3 sentences, the question is "
        "always last, and only ONE easy question.",
        f'Greeting: "{greeting}". Describe the sender only as "pravim sajtove za lokalne firme" '
        '(never "pravim i unapređujem").',
        "Tone: natural, spoken, warm, modest, curious. Not an ad, not a template. The question sounds like "
        "genuine interest. No emoji, no exclamation marks, no superlatives, no sales phrases, no price, "
        "no offer of services.",
        'Serbian, Latin script with diacritics (even if the company name is Cyrillic). Polite "Vi" form, '
        'always capitalized: Vaš, Vam, Vas, Vaši. Avoid "Vam" and "Vas" in the same sentence '
        '("kako Vam novi klijenti najčešće stižu?" or "kako novi klijenti najčešće dolaze do Vas?").',
        "Praise only what the operator's note or the bio says (photos, videos, before/after, experience, a "
        "concrete service, a slogan). The operator's own wording sets the strength: \"dobre slike\" stays "
        '"dobre". If there is no note and nothing concrete, use the plain "sviđa mi se kako radite" and put '
        '"a concrete detail would be better" in verify_note. If the profile looks weak, praise the service '
        "from the bio or leave the praise out. Never invent details.",
        "Never mention the Google rating or review count. Never write that you could not find their site or "
        "that they have none; a gap in the website is surfaced only through the question. Never criticize the "
        "design, speed or age of an existing site. Never mention Lighthouse numbers or audit scores.",
        "Exception, friendly warning: ONLY if the operator's note or the audit notes state a concrete, checkable "
        'problem (bio link dead, site title "My WordPress", not mobile-friendly), state it as a helpful heads-up '
        'before the question ("Primetio sam da link u bio-u ne radi, pa sam hteo da Vas upozorim jer verovatno '
        'gubite klijente koji kliknu."), and tell the sender in verify_note to check it before sending.',
        f"Question: pick by business type (niche: {niche}). Options: open (\"kako Vam novi klijenti najčešće "
        'stižu?"); Google (\"da li Vas klijenti pronalaze i preko Google-a, kad traže [concrete service], ili '
        'uglavnom stižu preko Instagrama i preporuke?\", strongest for services people search for); price and '
        'term (\"da li Vam se desi da neko pita za cenu ili termin, a da se razgovor u porukama izgubi?\"); '
        'if the lead already has a website (\"da li Vam sajt trenutno donosi nove klijente ili uglavnom stižu '
        'preko Instagrama i preporuke?\"); or a specific angle from the bio (photo estimates, coming to the '
        "address, work with companies, a school and its students). Adapt to the trade: a shop asks about "
        "online ordering, a self-service wash about prices, locations, packages and companies, a school about "
        "students. If the business is not detailing, do not copy the detailing template.",
    ]
    if lead.sm_audit:
        rules.append(
            "The operator's note is first-hand and trusted: base the praise on it, and do not mention "
            "that a note or audit exists."
        )

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
    if channel == "Facebook":
        rules.append('Facebook posts reach a page admin: after the question add a short line asking to pass the message to the owner if someone else reads it.')
    if outreach["link_in_first_message"]:
        rules.append(f'Add one short optional closing line pointing to "{outreach["site_link"]}".')
    else:
        rules.append(
            "Do NOT include any link or website address of the studio"
            + (" (the email signature below is the only exception)." if channel == "Email" else ".")
        )
    if outreach["named_references"]:
        rules.append(f'You may mention at most one of these past clients by name: {", ".join(outreach["named_references"])}.')
    else:
        rules.append('Do NOT name any past client or portfolio site; if relevant describe past work by industry only. No testimonials.')

    return (
        f"Write ONE first message for the channel: {channel}.\n\n"
        "LEAD DATA (everything you may use):\n"
        f"- Business: {lead.company}\n- Niche: {niche} (industry key: {lead.industry})\n"
        f"- Location: {lead.location or 'unknown'}\n"
        f"- Website: {lead.domain or 'none found'}\n"
        f"- Instagram: {socials.get('instagram', '')}  Facebook: {socials.get('facebook', '')}\n"
        f"- Audit notes (for a friendly warning only, never for criticism): {lead.audit_notes or 'none'}\n"
        f"- Operator's note (written by the sender; the only source of praise, base the "
        f"concrete point on it): {lead.sm_audit or 'none'}\n"
        f"- Homepage text:\n{lead.site_text or '(not available)'}\n\n"
        "RULES:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)) + "\n\n"
        "Return `message` (the text to send) and `verify_note`: one sentence listing any claim "
        "taken from the homepage text, bio or audit that the sender should double-check before "
        "sending, or an empty string if every fact is from the structured data above.\n\n"
        "VOICE REFERENCES (tone and structure only, never copy):\n" + examples
    )


class DraftBudgetError(RuntimeError):
    """The claude CLI refused because credits or the usage limit ran out.
    Unlike a per-lead failure, every further call would fail the same way, so
    draft_lead re-raises it and the stage stops instead of burning the batch."""


# wording seen from the CLI / API when an account is out of budget; matched
# loosely on purpose — a false positive only pauses, and drafting resumes later
_BUDGET_RE = re.compile(
    r"usage limit|rate limit|credit balance|limit reached|out of credits|quota|insufficient credit",
    re.IGNORECASE,
)


def _call_claude(prompt: str, model: str, timeout_s: int) -> dict:
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
                "--model", model,
                "--output-format", "json",
                "--json-schema", json.dumps(_SCHEMA),
                "--tools", "",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--setting-sources", "",
                "--system-prompt", _SYSTEM,
            ],
            capture_output=True, text=True, timeout=timeout_s, cwd=cwd,
        )
    if proc.returncode != 0:
        detail = f"{proc.stderr.strip()} {proc.stdout.strip()}"
        if _BUDGET_RE.search(detail):
            raise DraftBudgetError(detail.strip()[:200])
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    out = json.loads(proc.stdout).get("structured_output")
    if not isinstance(out, dict):
        raise RuntimeError("claude returned no structured_output")
    return out


def draft_lead(
    lead: Lead, outreach: dict, draft_cfg: dict, variant: int = 0, examples: str = ""
) -> Lead:
    """Return a new Lead with suggested_channel/draft_message/draft_notes filled.
    No usable contact data -> returned unchanged. A failure lands in Lead.errors instead
    of raising, so one bad call doesn't halt the batch — except DraftBudgetError
    (out of credits), which propagates."""
    channel = lead.suggested_channel or suggest_channel(lead)
    if not channel:
        return lead  # nothing to send to; the runner counts these, no error recorded
    prompt = build_prompt(lead, channel, outreach, variant, examples)
    try:
        raw = _call_claude(prompt, draft_cfg["model"], draft_cfg["timeout_s"])
        problems = validate_message(str(raw.get("message", "")), channel)
        if problems:  # one retry with the violation named
            retry = prompt + "\n\nYour previous attempt was rejected: " + "; ".join(problems) + ". Fix it."
            raw = _call_claude(retry, draft_cfg["model"], draft_cfg["timeout_s"])
            problems = validate_message(str(raw.get("message", "")), channel)
        if problems:
            raise RuntimeError("; ".join(problems))
    except DraftBudgetError:
        raise
    except Exception as e:
        return replace(lead, suggested_channel=channel, errors=[*lead.errors, f"draft: {e}"])
    return replace(
        lead,
        suggested_channel=channel,
        draft_message=str(raw["message"]).strip(),
        draft_notes=str(raw.get("verify_note", "")).strip(),
    )
