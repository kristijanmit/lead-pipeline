"""Suggested outreach channel — pure function, no I/O.

Priority (the draft-outreach skill's rules): Instagram, Facebook, Viber/Whatsapp
(mobile number and no Instagram/Facebook), Email, Call (landline only). Placeholder
contact data is ignored. Returns "" when the lead has no usable contact data.
"""

from __future__ import annotations

import re

from pipeline.schema import Lead


def _digits(phone: str) -> str:
    d = re.sub(r"\D", "", phone)
    return d[2:] if d.startswith("00") else d


def is_placeholder_phone(phone: str) -> bool:
    d = _digits(phone)
    tail = d[-7:] if len(d) >= 7 else d
    return (
        len(set(tail)) <= 1  # 0000000, 1111111
        or tail in "01234567890123456789"  # 1234567-style runs
        or re.search(r"(?:0{4,}|1234567)", d) is not None
    )


def is_serbian_mobile(phone: str) -> bool:
    d = _digits(phone)
    return d.startswith("3816") or (d.startswith("06") and not d.startswith("060000"))


def is_placeholder_email(email: str) -> bool:
    local, _, domain = email.lower().partition("@")
    return local in {"test", "example", "email", "your"} or domain in {"example.com", "domain.com", "email.com"}


def suggest_channel(lead: Lead) -> str:
    socials = lead.social_links
    if socials.get("instagram"):
        return "Instagram"
    if socials.get("facebook"):
        return "Facebook"
    phones = [p for p in lead.contact_phones if not is_placeholder_phone(p)]
    if any(is_serbian_mobile(p) for p in phones):
        return "Viber/Whatsapp"
    if any(not is_placeholder_email(e) for e in lead.contact_emails):
        return "Email"
    if phones:
        return "Call"
    return ""
