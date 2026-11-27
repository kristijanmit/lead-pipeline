"""Outreach drafting — one LLM call per lead, via the `claude` CLI.

Drafts are text for a human to review and send; nothing here sends anything.
The CLI call is isolated in _call_claude() so swapping it for an API-key
client later is a one-function change. The call runs with skills, MCP servers,
and settings disabled and from an empty temp dir (no CLAUDE.md pickup) —
without that the CLI loads ~135k tokens of context per call instead of ~1k.

The copy rules are the operator's ONIX outreach instructions (docs/OUTREACH.md), one
rule set per language: rules_sr.py, rules_en.py; examples per language in
config/outreach.{language}.md. Hard rules the model can break are re-checked in code
by rules.validate_message();
a violation gets one retry with the problem named, then lands in Lead.errors.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from pipeline.drafting import rules_en, rules_sr
from pipeline.drafting.channel import suggest_channel
from pipeline.drafting.rules import MAX_CHARS, Rules, validate_connection_note, validate_message
from pipeline.market import Market
from pipeline.niche import Niche
from pipeline.schema import Lead

__all__ = ["MAX_CHARS", "RULE_SETS", "build_prompt", "draft_lead", "rules_for", "select_examples", "validate_message"]

# one rule set per drafting language (script_edits.md §9.2); the market picks it
RULE_SETS: dict[str, Rules] = {"sr": rules_sr.RULES, "en": rules_en.RULES}

# channels whose examples have a "with audit offer" section; elsewhere first_offer
# "audit" falls back to the question
AUDIT_CHANNELS = ("Instagram",)
# lead types with a site the audit note can be about (new_build leads keep the question)
_AUDITED_TYPES = ("redesign", "unclear")


def offer_for(lead: Lead, channel: str, outreach: dict) -> str:
    """The first message's offer: "audit" only when `outreach.first_offer` is audit, the
    channel has audit-offer examples, and the lead has an audited site that answered
    (lead_type redesign / unclear, site_reachable). Everything else: "question"."""
    if (
        outreach.get("first_offer") == "audit"
        and channel in AUDIT_CHANNELS
        and lead.lead_type in _AUDITED_TYPES
        and lead.site_reachable is True
    ):
        return "audit"
    return "question"

_SCHEMA = {
    "type": "object",
    "required": ["message", "verify_note"],
    "properties": {
        "message": {"type": "string"},
        "verify_note": {"type": "string"},
    },
}
# LinkedIn has two outputs: the connection request note and the message after accept
_LINKEDIN_SCHEMA = {
    "type": "object",
    "required": ["connection_note", "message", "verify_note"],
    "properties": {
        "connection_note": {"type": "string"},
        "message": {"type": "string"},
        "verify_note": {"type": "string"},
    },
}


def rules_for(language: str) -> Rules:
    try:
        return RULE_SETS[language]
    except KeyError:
        raise ValueError(f"no drafting rules for language {language!r}") from None


_COMMENT_RE = re.compile(r"<!--.*?-->\n?", re.DOTALL)


def load_examples(path: str) -> str:
    """The examples file, HTML comments (notes for the operator) removed; "" if missing."""
    p = Path(path)
    return _COMMENT_RE.sub("", p.read_text(encoding="utf-8")) if p.exists() else ""


def examples_path(outreach: dict, language: str) -> str:
    return outreach["examples_path"].format(language=language)


_DM_CHANNELS = ("Instagram", "Facebook", "Viber/Whatsapp")
_META_ALIASES = ("generic", "connection note", "after accept")


def _split(text: str, marker: str) -> tuple[str, list[tuple[str, str]]]:
    """Text before the first `marker` heading, then (heading, body) pairs."""
    parts = re.split(rf"(?m)^{marker} +(.+)$", text)
    return parts[0], list(zip(parts[1::2], parts[2::2]))


def _aliases(heading: str) -> list[str]:
    return [a.strip() for a in heading.lower().split("|") if a.strip()]


def select_examples(
    text: str, channel: str, niche: str, redesign: bool = False, offer: str = "question"
) -> str:
    """Narrow the examples file to what fits this lead. `## <channel>` sections
    are picked by the lead's channel (Viber/Whatsapp reads its own section, else
    the Instagram DM one); sections whose heading starts with "Questions" or "Friendly warning"
    ride along. Inside a section, `### a | b` subsections are kept when one alias
    matches the lead's niche, else only the ones with a `generic` alias. For Viber/Whatsapp a
    `## Viber/Whatsapp (redesign)` section serves leads that already have a site
    (redesign=True), the plain one the rest; either falls back to the other.
    "... with audit offer" sections are used only when offer == "audit" on an
    AUDIT_CHANNELS channel, and then replace the plain section. LinkedIn always keeps
    its `connection note` subsection. A file without these headings is returned unchanged."""
    preamble, sections = _split(text, "##")
    if not sections:
        return text
    audit = offer == "audit" and channel in AUDIT_CHANNELS
    offer_sections = [(h, b) for h, b in sections if "with audit offer" in h.lower()]
    sections = [x for x in sections if x not in offer_sections]
    wanted = {
        "Viber/Whatsapp": ("viber", "instagram"), "Call": ("sms", "call"),
    }.get(channel, (channel.lower(),))
    own = [(h, b) for h, b in sections if h.lower().startswith(wanted[0])]
    if audit:
        own = [x for x in offer_sections if x[0].lower().startswith(wanted[0])] or own
    if channel == "Viber/Whatsapp":
        fits = [(h, b) for h, b in own if ("(redesign)" in h.lower()) == redesign]
        own = fits or own
    for prefix in wanted[1:]:  # fallback prefixes, only used when nothing matched yet
        own = own or [(h, b) for h, b in sections if h.lower().startswith(prefix)]
    if not own:
        return text
    shared = [
        (h, b) for h, b in sections
        if h.lower().startswith(("questions", "friendly warning")) or " with a friendly warning" in h.lower()
    ]
    if own[0][0].lower().startswith("viber"):
        shared = []  # its format has its own closing offer; no extra question/warning sections
    elif channel not in _DM_CHANNELS:
        shared = [(h, b) for h, b in shared if h.lower().startswith("questions")]
    out = [preamble.strip()]
    keys = niche.lower().strip()
    for heading, body in own + [x for x in shared if x not in own]:
        intro, subs = _split(body, "###")
        if subs:
            hit = [
                (h, b) for h, b in subs
                if any(a not in _META_ALIASES and (a in keys or keys in a) for a in _aliases(h))
            ] if keys else []
            generic = [(h, b) for h, b in subs if "generic" in _aliases(h)]
            always = [(h, b) for h, b in subs if channel == "LinkedIn" and "connection note" in _aliases(h)]
            picked = always + [x for x in (hit or generic or subs) if x not in always]
            body = intro + "".join(f"### {h}{b}" if always else b for h, b in picked)
        out.append(f"## {heading}{body}".rstrip())
    return "\n\n".join(x for x in out if x) + "\n"


def _question_rule(channel: str, niche_phrase: str, niche: Niche | None, offer: str) -> str:
    """Niche-neutral (script_edits.md §10.1): the niche's own question from `go`, else one
    from the examples' Questions section. Same wording for every language."""
    if offer == "audit":
        return (
            "Offer instead of a question: the last sentence offers to look at how their clients get from a phone "
            "or Google to a booking and send a one-page note with three concrete things to improve, phrased as a "
            "question (follow the \"with audit offer\" references). Describe the work, never price it."
        )
    if niche and niche.question:
        return (
            f'Question: use the niche\'s own question, adapted to this business and written in the message '
            f'language: "{niche.question}".'
        )
    return (
        f"Question: pick one from the Questions section of the references and adapt it to this business "
        f"(niche: {niche_phrase}). Never copy one trade's wording to another; if the lead already has a "
        "website, the 'already has a site' question fits best."
    )


def build_prompt(
    lead: Lead, channel: str, outreach: dict, variant: int, examples: str,
    rules: Rules | None = None, niche: Niche | None = None, market: Market | None = None,
) -> str:
    # `variant` is unused (one fixed descriptor and a question rule); kept so the
    # runner call sites and signature stay stable.
    rules = rules or rules_for(lead.language)
    socials = lead.social_links
    # the run's draft phrase; pages without Niche Label fall back to the niche name in Industry
    phrase = lead.niche_label or lead.industry
    offer = offer_for(lead, channel, outreach)
    rules_list = rules.prompt_rules(
        lead, channel, outreach, phrase, _question_rule(channel, phrase, niche, offer), offer,
        review_strong=market.review_strong if market else None,
    )
    if lead.sm_audit:
        rules_list.append(
            "The operator's note is first-hand and trusted: base the praise on it, and do not mention "
            "that a note or audit exists."
        )
    link = (niche.landing_url if niche and niche.landing_url else "") or outreach["site_link"]
    if channel == "Viber/Whatsapp":
        pass  # the Viber rules already say which link, if any, to use
    elif outreach["link_in_first_message"]:
        rules_list.append(f'Add one short optional closing line pointing to "{link}".')
    else:
        rules_list.append(
            "Do NOT include any link or website address of the studio"
            + (" (the email signature below is the only exception)." if channel == "Email" else ".")
        )
    if outreach["named_references"]:
        rules_list.append(f'You may mention at most one of these past clients by name: {", ".join(outreach["named_references"])}.')
    else:
        rules_list.append('Do NOT name any past client or portfolio site; if relevant describe past work by industry only. No testimonials.')

    if channel == "LinkedIn":
        rules_list.append(rules.connection_note_rule)
        outputs = (
            "Return `connection_note` (the LinkedIn connection request note, max 300 characters), `message` "
            "(the first message after they accept) and `verify_note`"
        )
    else:
        outputs = "Return `message` (the text to send) and `verify_note`"
    return (
        f"Write ONE first message for the channel: {channel}.\n\n"
        "LEAD DATA (everything you may use):\n"
        f"- Business: {lead.company}\n- Niche: {phrase} (industry key: {lead.industry})\n"
        f"- Location: {lead.location or 'unknown'}\n"
        f"- Website: {lead.domain or 'none found'}\n"
        f"- Instagram: {socials.get('instagram', '')}  Facebook: {socials.get('facebook', '')}\n"
        f"- Audit notes (for a friendly warning only, never for criticism): {lead.audit_notes or 'none'}\n"
        f"- Operator's note (written by the sender; the only source of praise, base the "
        f"concrete point on it): {lead.sm_audit or 'none'}\n"
        f"- Homepage text:\n{lead.site_text or '(not available)'}\n\n"
        "RULES:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules_list, 1)) + "\n\n"
        f"{outputs}: one sentence listing any claim "
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


def _call_claude(
    prompt: str, model: str, timeout_s: int, *, system: str = rules_sr.SYSTEM, schema: dict = _SCHEMA
) -> dict:
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            [
                "claude", "-p", prompt,
                "--model", model,
                "--output-format", "json",
                "--json-schema", json.dumps(schema),
                "--tools", "",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--setting-sources", "",
                "--system-prompt", system,
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


DRAFT_LANGUAGES = tuple(RULE_SETS)


def language_skip_reason(market: Market) -> str:
    """Why leads in this market can't be drafted ("" = they can). Callers skip
    and count such leads; a draft in the wrong language is never written."""
    if market.language in DRAFT_LANGUAGES:
        return ""
    return f"no drafting rules for language {market.language!r}"


def reviews_ok(lead: Lead, market: Market) -> bool:
    """Enough reviews to cite them ("dosta ocena"): at least the market's review_strong."""
    return (lead.review_count or 0) >= market.review_strong


def _problems(raw: dict, channel: str, rules: Rules, outreach: dict, cite_reviews: bool) -> list[str]:
    problems = validate_message(
        str(raw.get("message", "")), channel, rules,
        allow_link=bool(outreach.get("link_in_first_message")), reviews_ok=cite_reviews,
    )
    if channel == "LinkedIn":
        problems += validate_connection_note(str(raw.get("connection_note", "")), rules)
    return problems


def _compose(raw: dict, channel: str) -> str:
    message = str(raw["message"]).strip()
    if channel == "LinkedIn":
        note = str(raw.get("connection_note", "")).strip()
        return f"Connection note:\n{note}\n\nAfter accept:\n{message}"
    return message


def draft_lead(
    lead: Lead, outreach: dict, draft_cfg: dict, market: Market, variant: int = 0, examples: str = "",
    niche: Niche | None = None,
) -> Lead:
    """Return a new Lead with suggested_channel/draft_message/draft_notes filled.
    No usable contact data -> returned unchanged. A failure lands in Lead.errors instead
    of raising, so one bad call doesn't halt the batch — except DraftBudgetError
    (out of credits), which propagates. The channel follows `market`'s rules, the rule
    set its language; `examples` is that language's examples file. `niche` (the run's
    definition) supplies the niche question and landing URL when known. A market
    whose language has no rule set raises ValueError: callers check
    language_skip_reason() first and skip those leads. LinkedIn drafts hold both
    outputs, labelled "Connection note:" and "After accept:"."""
    reason = language_skip_reason(market)
    if reason:
        raise ValueError(f"cannot draft for market {market.id!r}: {reason}")
    rules = rules_for(market.language)
    channel = lead.suggested_channel or suggest_channel(lead, market)
    if not channel:
        return lead  # nothing to send to; the runner counts these, no error recorded
    selected = select_examples(
        examples, channel, lead.niche or lead.industry, redesign=bool(lead.domain),
        offer=offer_for(lead, channel, outreach),
    )
    prompt = build_prompt(lead, channel, outreach, variant, selected, rules=rules, niche=niche, market=market)
    cite_reviews = reviews_ok(lead, market)
    call = {"system": rules.system, "schema": _LINKEDIN_SCHEMA if channel == "LinkedIn" else _SCHEMA}
    try:
        raw = _call_claude(prompt, draft_cfg["model"], draft_cfg["timeout_s"], **call)
        problems = _problems(raw, channel, rules, outreach, cite_reviews)
        if problems:  # one retry with the violation named
            retry = prompt + "\n\nYour previous attempt was rejected: " + "; ".join(problems) + ". Fix it."
            raw = _call_claude(retry, draft_cfg["model"], draft_cfg["timeout_s"], **call)
            problems = _problems(raw, channel, rules, outreach, cite_reviews)
        if problems:
            raise RuntimeError("; ".join(problems))
    except DraftBudgetError:
        raise
    except Exception as e:
        return replace(lead, suggested_channel=channel, errors=[*lead.errors, f"draft: {e}"])
    return replace(
        lead,
        suggested_channel=channel,
        draft_message=_compose(raw, channel),
        draft_notes=str(raw.get("verify_note", "")).strip(),
    )
