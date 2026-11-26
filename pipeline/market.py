"""Market profiles — facts about a country the pipeline works in (docs/plan/v2,
strategy_gaps.md K2): its phone rules, drafting language, channel order.

Presets live in config.yaml under `markets:`; `go` can also create one on the
spot. The chosen market is written into the run manifest and stamped on every
lead, so later stages never fall back to a default country.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

LANGUAGES = ("sr", "en")
KNOWN_CHANNELS = ("Instagram", "Facebook", "Viber/Whatsapp", "WhatsApp", "LinkedIn", "Email", "Call")

_REQUIRED = ("country", "dial_code", "mobile_prefixes", "language", "channels")


@dataclass
class Market:
    id: str
    country: str
    dial_code: str  # e.g. "+381"
    mobile_prefixes: list[str]  # e.g. ["+3816"]
    language: str  # drafting rule set + examples file: "sr" | "en"
    channels: list[str]  # first-touch channel order, best first
    first_touch_excluded: list[str] = field(default_factory=list)
    review_strong: int = 50  # review count from which viability counts as strong
    currency: str = ""
    maps_language: str = ""
    exclude_name_patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, market_id: str, data: dict) -> Market:
        """Build and validate; raises ValueError naming the market and the problem."""
        if not isinstance(data, dict):
            raise ValueError(f"market {market_id!r} must be a mapping")
        missing = [k for k in _REQUIRED if not data.get(k)]
        if missing:
            raise ValueError(f"market {market_id!r} is missing {', '.join(missing)}")
        unknown = set(data) - {f for f in cls.__dataclass_fields__}
        if unknown:
            raise ValueError(f"market {market_id!r} has unknown keys {sorted(unknown)}")
        values = {k: v for k, v in data.items() if k != "id"}
        market = cls(id=market_id, **values)
        market.validate()
        return market

    def validate(self) -> None:
        name = f"market {self.id!r}"
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("market id must be a non-empty string")
        for key in ("country", "dial_code", "language"):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError(f"{name}: {key} must be a non-empty string")
        if not self.dial_code.startswith("+"):
            raise ValueError(f"{name}: dial_code must start with + (e.g. +381)")
        if self.language not in LANGUAGES:
            raise ValueError(f"{name}: language must be one of {', '.join(LANGUAGES)}")
        for key in ("mobile_prefixes", "channels", "first_touch_excluded", "exclude_name_patterns"):
            value = getattr(self, key)
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ValueError(f"{name}: {key} must be a list of strings")
        if not self.mobile_prefixes or not self.channels:
            raise ValueError(f"{name}: mobile_prefixes and channels must not be empty")
        bad = [c for c in self.channels if c not in KNOWN_CHANNELS]
        if bad:
            raise ValueError(f"{name}: unknown channels {bad} (known: {', '.join(KNOWN_CHANNELS)})")
        extra = [c for c in self.first_touch_excluded if c not in self.channels]
        if extra:
            raise ValueError(f"{name}: first_touch_excluded {extra} must also be in channels")
        if not isinstance(self.review_strong, int) or self.review_strong <= 0:
            raise ValueError(f"{name}: review_strong must be a positive integer")
        for key in ("currency", "maps_language"):
            if not isinstance(getattr(self, key), str):
                raise ValueError(f"{name}: {key} must be a string")


def load_market(config: dict, market_id: str) -> Market:
    """The preset `markets.<id>` from config (already validated at load)."""
    markets = config.get("markets") or {}
    key = (market_id or "").strip().lower()
    if key not in markets:
        known = ", ".join(markets) or "none configured"
        raise ValueError(f"unknown market {market_id!r} — configured: {known}")
    return Market.from_dict(key, markets[key])


def _split(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


def market_from_prompts(ask: Callable[[str, str], str]) -> Market:
    """A new market from `go` prompts. `ask(question, default)` returns the answer
    (re-asking empty required answers is the caller's job, as in runner._ask)."""
    market_id = ask("Market id (short, e.g. croatia)", "").strip().lower()
    country = ask("Country (as written on the leads, e.g. Croatia)", "")
    dial_code = ask("Dialing code (e.g. +385)", "")
    prefixes = _split(ask("Mobile prefixes incl. dialing code, comma-separated (e.g. +3859)", ""))
    language = ask("Drafting language (sr / en)", "en").strip().lower()
    channels = _split(ask(
        f"Channel order, best first ({', '.join(KNOWN_CHANNELS)})",
        "Instagram,Facebook,Email,Call",
    ))
    raw_excluded = ask("Channels never used for a first touch (comma-separated, or -)", "-")
    excluded = [] if raw_excluded.strip() == "-" else _split(raw_excluded)
    currency = ask("Currency (e.g. EUR)", "EUR")
    maps_language = ask("Google Maps language code (e.g. hr)", language)
    market = Market(
        id=market_id, country=country, dial_code=dial_code, mobile_prefixes=prefixes,
        language=language, channels=channels, first_touch_excluded=excluded,
        currency=currency, maps_language=maps_language,
    )
    market.validate()
    return market


def market_yaml(market: Market) -> str:
    """The preset as a config.yaml snippet to paste under `markets:` — never
    written to config automatically."""
    import yaml

    body = {k: v for k, v in market.to_dict().items() if k != "id"}
    return yaml.safe_dump({"markets": {market.id: body}}, allow_unicode=True, sort_keys=False)
