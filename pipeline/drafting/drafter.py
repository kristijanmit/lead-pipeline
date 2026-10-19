"""Outreach drafting — one LLM call per lead, via the `claude` CLI.

Drafts are text for a human to review and send; nothing here sends anything.
The CLI call is isolated in _call_claude() so swapping it for an API-key
client later is a one-function change. The call runs with skills, MCP servers,
and settings disabled and from an empty temp dir (no CLAUDE.md pickup) —
without that the CLI loads ~135k tokens of context per call instead of ~1k.

The copy rules below are the former draft-outreach skill's (docs/OUTREACH.md).
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

_DESCRIPTORS = (
    "pravim i unapređujem sajtove za lokalne firme",
    "bavim se izradom i unapređenjem sajtova za lokalne firme",
    "pravim sajtove za lokalne firme",
)
_QUESTION_STYLES = (
    'kako Vam danas stižu upiti: telefonom, mejlom ili Viberom?',
    'da li Vam novi klijenti stižu preko sajta ili većinom preporukom?',
    'kako klijenti najčešće dođu do Vas: preko interneta ili preko preporuke?',
)

# phrases the operator never wants, checked case-insensitively
_BANNED = ("bez obaveze", "besplatno", "ove nedelje", "lighthouse", "seo ")

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
    if channel == "Email" and not low.startswith("subject:"):
        problems.append("email must start with a 'Subject:' line")
    if channel not in ("Email", "Call") and "subject:" in low:
        problems.append("chat messages must not have a subject line")
    return problems


def load_examples(path: str) -> str:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def build_prompt(lead: Lead, channel: str, outreach: dict, variant: int, examples: str) -> str:
    socials = lead.social_links
    niche = outreach["industry_labels"].get(lead.industry, lead.industry)
    descriptor = _DESCRIPTORS[0] if lead.lead_type == "new_build" else _DESCRIPTORS[variant % 3]
    question_style = _QUESTION_STYLES[variant % 3]

    rules = [
        f'Opening: "Dobar dan, ja sam Kristijan, {descriptor}". Chat channels: first name only, '
        "no company name for the sender, no signature.",
        "Then ONE genuine, specific point about the business: its specialty, years in business, "
        "a client it lists, its tagline, or rating plus review count (only if the rating is strong). "
        "Real fact only — never a score.",
        "At most ONE observation about their web presence, in plain words, only if true and useful "
        '(site slow, not mobile-friendly, very old WordPress, leftover template text). If it comes only '
        'from the audit data, write "Primetio sam da ...". Never quote Lighthouse numbers or jargon.',
        f'End with ONE easy open question about how their business works (style hint: "{question_style}"). '
        'Not "do you want X?". It is a conversation starter, NOT a pitch: no price, no package, '
        "no call request, no attachment, no offer.",
        'Serbian, informal-polite "Vi" form, Latin script (even if the company name is Cyrillic), '
        'correct grammar ("naišao sam na", not "našao sam na"). No exclamation marks.',
        "Never use: Lighthouse scores, \"bez obaveze\", \"besplatno\", any date/time-relative phrase "
        "(\"ove nedelje\"), unsupportable superlatives, comparisons with competitors.",
        "Never invent facts. If data is thin, write a shorter, more general message. If audit data "
        "conflicts (e.g. CMS vs notes), leave that fact out.",
    ]
    if lead.sm_audit:
        rules.append(
            "The operator's social media audit is first-hand and trusted: take the ONE specific point "
            "from it (not from ratings), and do not mention that an audit exists."
        )
    if lead.lead_type == "new_build":
        rules.append("Angle: no website found. Compliment the rating/specialty, say you could not find a site, ask how inquiries arrive. Do not propose a call.")
    elif lead.lead_type == "redesign":
        rules.append("Angle: one true observation plus the workflow question. If the site has no clear fixable problem, do NOT criticize; use their specialty and ask about the inquiry workflow.")
    else:
        rules.append("Angle: short and curious; never claim anything about a site that could not be audited.")

    if channel == "Email":
        rules.append(
            'Format: first line "Subject: <short, specific>", blank line, body of 70-110 words, a one-line '
            'opt-out ("Ako Vam ovo nije od interesa, slobodno mi recite i neću Vam više pisati."), then the '
            f'signature "{outreach["signature"]}".'
        )
    elif channel == "Call":
        rules.append("Format: a short call script: opener, the one specific point, the question.")
    else:
        rules.append("Format: 2-4 short conversational sentences.")
    if channel == "Facebook":
        rules.append('Facebook posts reach a page admin: add a short line asking to pass the message to the owner if someone else reads it.')
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
        f"- Website: {lead.domain or 'none found'}; lead type: {lead.lead_type or 'unclear'}\n"
        f"- Rating: {lead.rating if lead.rating is not None else 'n/a'} from {lead.review_count or 0} reviews\n"
        f"- Instagram: {socials.get('instagram', '')}  Facebook: {socials.get('facebook', '')}\n"
        f"- Audit notes: {lead.audit_notes or 'none'}\n"
        f"- Operator's social media audit (written by the sender; primary context, base the "
        f"specific point on it): {lead.sm_audit or 'none'}\n"
        f"- Homepage text:\n{lead.site_text or '(not available)'}\n\n"
        "RULES:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)) + "\n\n"
        "Return `message` (the text to send) and `verify_note`: one sentence listing any claim "
        "taken from the homepage text or audit that the sender should double-check before "
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
