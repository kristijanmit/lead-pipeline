"""Outreach channels per market — pure functions, no I/O.

The one place that decides which contact channels a lead has and which one to
use first (docs/plan/v2/PIPELINE.md, "one source per rule"). Drafting uses it
now; the v4 scorer imports it for reach, so the two can't drift apart.

Order, mobile prefixes and first-touch exclusions come from the lead's
`Market` (config.yaml `markets:`). Channel labels are the market's own: the
mobile channel is "Viber/Whatsapp" in Serbia and "WhatsApp" in the UAE.
Placeholder phones and emails never count as a channel.
"""

from __future__ import annotations

import re

from pipeline.market import Market
from pipeline.schema import Lead

MOBILE_CHANNELS = ("Viber/Whatsapp", "WhatsApp")
_SOCIAL_CHANNELS = {"Instagram": "instagram", "Facebook": "facebook", "LinkedIn": "linkedin"}


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


def is_placeholder_email(email: str) -> bool:
    local, _, domain = email.lower().partition("@")
    return local in {"test", "example", "email", "your"} or domain in {"example.com", "domain.com", "email.com"}


def normalize_phone(phone: str, market: Market) -> str:
    """`+<digits>`: `00…` is international, a single leading `0` is a local
    number in the market (`050…` in the UAE -> `+97150…`). Anything else is
    taken as already carrying its country code."""
    d = re.sub(r"\D", "", phone)
    if d.startswith("00"):
        d = d[2:]
    elif d.startswith("0"):
        d = _digits(market.dial_code) + d[1:]
    return "+" + d if d else ""


def is_mobile(phone: str, market: Market) -> bool:
    number = normalize_phone(phone, market)
    return bool(number) and any(number.startswith("+" + _digits(p)) for p in market.mobile_prefixes)


def usable_channels(lead: Lead, market: Market) -> list[str]:
    """Every channel this lead can be reached on, in the market's order —
    first-touch exclusions included (they stay visible, best_channel skips them)."""
    phones = [p for p in lead.contact_phones if not is_placeholder_phone(p)]
    has = {
        **{ch: bool(lead.social_links.get(key)) for ch, key in _SOCIAL_CHANNELS.items()},
        **{ch: any(is_mobile(p, market) for p in phones) for ch in MOBILE_CHANNELS},
        "Email": any(not is_placeholder_email(e) for e in lead.contact_emails),
        "Call": bool(phones),
    }
    return [ch for ch in market.channels if has.get(ch)]


def best_channel(lead: Lead, market: Market) -> str:
    """The first-touch channel, or "" when the lead has no usable contact data."""
    excluded = set(market.first_touch_excluded)
    return next((ch for ch in usable_channels(lead, market) if ch not in excluded), "")
