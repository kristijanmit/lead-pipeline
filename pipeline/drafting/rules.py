"""Drafting rule sets per language — the shape both `rules_sr` and `rules_en` fill in,
plus the deterministic validator that re-checks what a model can silently break.

A rule set is data (banned phrases, greetings, script check…) plus one function that
writes the language-specific prompt rules. drafter.py picks the set by the lead's
language and never hard-codes a language itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

MAX_CHARS = 2000
CONNECTION_NOTE_MAX = 300  # LinkedIn's limit for a connection request note

# Never mention the patient-records platform (strategy_gaps.md S23). Both languages,
# whole word / phrase, case-insensitive.
PLATFORM_TERMS = (
    "patient records", "patient-records", "medical records", "EHR", "EMR",
    "karton pacijenta", "kartoni pacijenata", "elektronski karton",
    "medicinska dokumentacija", "evidencija pacijenata",
)
_PLATFORM_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in PLATFORM_TERMS) + r")\b", re.IGNORECASE
)
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿]")
_SLOT_RE = re.compile(r"[{}]")  # an unfilled {slot} copied from the examples
# the clause right after "Ja sam / ovde / I'm / this is Kristijan," is the sender line ("Ja sam
# Kristijan, pravim sajtove za lokalne firme, pa…"); the Email signature "Kristijan, ONIX…" is not
_SENDER_RE = re.compile(
    r"\b(?:ja sam|ovde|i'm|i am|this is|it's)\s+Kristijan,\s*([^,.?!\n]*)", re.IGNORECASE
)
_LINK_RE = re.compile(
    r"https?://|www\.|\b[a-z0-9-]+\.(?:com|net|org|io|co|ae|rs|me|app|site)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class Rules:
    language: str  # "sr" | "en"
    system: str  # system prompt for the CLI call
    # (label, regex) pairs, matched case-insensitively; label is what the retry names
    banned: tuple[tuple[str, str], ...]
    # channel -> labels from `banned` that this channel's format may use (Serbian Viber)
    channel_allowed: dict[str, tuple[str, ...]]
    script_re: str  # characters that must not appear
    script_problem: str
    greetings: dict[str, str]  # channel -> required opening; "" key is the DM default
    rating_re: str = ""  # a rating mention outside `banned` (sr: "ocena", not "procena")
    rating_allowed: tuple[str, ...] = ()  # channels whose format may cite reviews
    # labels in channel_allowed that are review claims: allowed only when the lead has
    # at least market.review_strong reviews (reviews_ok)
    review_labels: tuple[str, ...] = ()
    # channel -> the approved sender clauses ("" key = default), compared case-insensitively;
    # anything added (a service, "online zakazivanje", "booking") is rejected
    sender_lines: dict[str, tuple[str, ...]] = field(default_factory=dict)
    max_questions: int | None = None  # None = not checked
    no_links: bool = False  # reject URLs/domains (Email's signature line is exempt)
    # (lead, channel, outreach, niche_phrase, question_rule, offer) -> list of prompt rules
    prompt_rules: Callable[..., list[str]] = field(default=lambda *a: [])
    connection_note_rule: str = ""
    plain_praise: str = ""

    def greeting(self, channel: str) -> str:
        return self.greetings.get(channel, self.greetings[""])

    def sender(self, channel: str) -> tuple[str, ...]:
        return self.sender_lines.get(channel, self.sender_lines.get("", ()))


def _banned_problems(text: str, channel: str, rules: Rules, reviews_ok: bool = True) -> list[str]:
    allowed = set(rules.channel_allowed.get(channel, ()))
    if not reviews_ok:
        allowed -= set(rules.review_labels)
    problems = [
        f"contains banned phrase {label!r}"
        for label, pattern in rules.banned
        if label not in allowed and re.search(pattern, text, re.IGNORECASE)
    ]
    if _PLATFORM_RE.search(text):
        problems.append("mentions the patient-records platform; never mention it")
    rating_ok = channel in rules.rating_allowed and reviews_ok
    if rules.rating_re and not rating_ok and re.search(rules.rating_re, text, re.IGNORECASE):
        problems.append("mentions a rating; never mention Google ratings or reviews")
    return problems


def _sender_problems(text: str, channel: str, rules: Rules) -> list[str]:
    approved = rules.sender(channel)
    if not approved:
        return []
    for clause in _SENDER_RE.findall(text):
        if clause.strip().lower() not in {a.lower() for a in approved}:
            wanted = " / ".join(repr(a) for a in approved)
            return [f"sender line must be exactly {wanted}, nothing added (got {clause.strip()!r})"]
    return []


def _common_problems(text: str, channel: str, rules: Rules, limit: int, reviews_ok: bool = True) -> list[str]:
    problems = []
    if len(text) >= limit:
        problems.append(f"message is {len(text)} chars, limit is {limit - 1}")
    if re.search(rules.script_re, text):
        problems.append(rules.script_problem)
    if "!" in text:
        problems.append("contains an exclamation mark")
    if _EMOJI_RE.search(text):
        problems.append("contains an emoji")
    if _SLOT_RE.search(text):
        problems.append("contains a {slot} from the examples; write real text")
    return problems + _banned_problems(text, channel, rules, reviews_ok) + _sender_problems(text, channel, rules)


def validate_message(
    message: str, channel: str, rules: Rules, allow_link: bool = False, reviews_ok: bool = True
) -> list[str]:
    """Deterministic re-check of the rules a model can silently break. For LinkedIn this
    is the after-accept message; the connection note goes through validate_connection_note.
    reviews_ok=False (fewer reviews than the market's review_strong) removes the review
    claims a channel's format otherwise allows (Serbian Viber "dosta ocena")."""
    text = message.strip()
    if not text:
        return ["message is empty"]
    problems = _common_problems(text, channel, rules, MAX_CHARS, reviews_ok)
    low = text.lower()
    body = low
    if channel == "Email" and low.startswith("subject:"):
        body = low.partition("\n")[2].strip()
    greeting = rules.greeting(channel)
    if not body.startswith(greeting.lower()):
        problems.append(f"must start with {greeting!r}")
    questions = text.count("?")
    if channel != "Call" and not questions:
        problems.append("must contain one question")
    if rules.max_questions is not None and questions > rules.max_questions:
        problems.append(f"contains {questions} questions, only {rules.max_questions} allowed")
    if rules.no_links and not allow_link:
        checked = text
        if channel == "Email":  # the signature (last line) carries the studio domain
            checked = text.rstrip().rpartition("\n")[0]
        if _LINK_RE.search(checked):
            problems.append("contains a link; no links in a first message")
    if channel == "Email" and not low.startswith("subject:"):
        problems.append("email must start with a 'Subject:' line")
    if channel not in ("Email", "Call") and "subject:" in low:
        problems.append("chat messages must not have a subject line")
    return problems


def validate_connection_note(note: str, rules: Rules) -> list[str]:
    """LinkedIn connection note: short, greeting first, no pitch; a question is optional."""
    text = note.strip()
    if not text:
        return ["connection note is empty"]
    problems = _common_problems(text, "LinkedIn", rules, CONNECTION_NOTE_MAX + 1)
    greeting = rules.greeting("LinkedIn")
    if not text.lower().startswith(greeting.lower()):
        problems.append(f"must start with {greeting!r}")
    if rules.no_links and _LINK_RE.search(text):
        problems.append("contains a link")
    return [f"connection note: {p}" for p in problems]
