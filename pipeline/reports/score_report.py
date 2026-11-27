"""score-report: calibration tables from Notion pages + the runs' JSONL (v2 step 10,
docs/plan/v2/PIPELINE.md §7, script_edits.md §11.6, strategy_gaps.md S19).

Pure functions, no I/O: the runner fetches the pages and the latest JSONL row per
page and passes them in. Everything a page says comes from what you did in
Notion: Status, Last Contacted, Communication, Outcome, Verdict, Skip Reason.

Page state (each page is at most one):
- sent: Status Sent / Follow-up due / Replied / Closed, Last Contacted set, a
  "· SENT ·" line in Communication, or Outcome set. Status Dead counts only next
  to Last Contacted, a SENT line or Outcome. Sent wins over Verdict = Skip (a Skip
  after sending only stops follow-ups).
- skipped: Verdict = Skip and nothing says it was sent.
- pending: a draft exists (Draft Message, or Status Queued / Draft failed), no
  decision yet. Shown, never in a rate.
- None: left out (not drafted yet, or Dead with no other sent signal and no Verdict).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TIERS = ("A", "B", "C", "D")

# Status values that only exist after a first message went out. Dead is not one:
# it counts only next to another sent signal (_DEAD below).
_SENT_STATUSES = {"Sent", "Follow-up due", "Replied", "Closed"}
_PENDING_STATUSES = {"Queued", "Draft failed"}
_REPLY_STATUSES = {"Replied", "Closed"}
# every Outcome except no_reply means they answered
_REPLY_OUTCOMES = {"declined", "replied", "meeting", "proposal", "won", "lost"}
_MEETING_OUTCOMES = {"meeting", "proposal", "won"}

# Communication log lines (Notion property description): "YYYY-MM-DD · SENT · channel · text"
# and "YYYY-MM-DD · REPLY · text"
_SENT_LINE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})\s*·\s*SENT\s*·\s*([^·]*?)\s*(?:·|$)", re.IGNORECASE)
_REPLY_LINE = re.compile(r"·\s*REPLY\s*(?:·|$)", re.IGNORECASE)

# typed channel names -> one label, so "instagram" and "Instagram DM" group together
_CHANNEL_ALIASES = {
    "instagram": "Instagram", "instagram dm": "Instagram", "ig": "Instagram",
    "facebook": "Facebook", "fb": "Facebook", "messenger": "Facebook",
    "viber/whatsapp": "Viber/Whatsapp", "viber": "Viber/Whatsapp",
    "whatsapp": "WhatsApp", "wa": "WhatsApp",
    "email": "Email", "e-mail": "Email", "mail": "Email",
    "call": "Call", "phone": "Call",
    "linkedin": "LinkedIn",
}

UNKNOWN = "(unknown)"


@dataclass
class PageRow:
    """One Notion page, reduced to what the report groups and counts."""

    page_id: str
    state: str  # sent | skipped | pending
    tier: str = UNKNOWN
    lead_type: str = UNKNOWN
    market: str = UNKNOWN
    niche: str = UNKNOWN
    confidence: str = UNKNOWN
    campaign: str = ""  # Notion Campaign select; "" on hand-made pages
    skip_reason: str = ""
    channel: str = UNKNOWN  # sent pages: latest SENT line, else Suggested Channel
    channel_fallback: bool = False  # True = channel came from Suggested Channel
    replied: bool = False
    meeting: bool = False


@dataclass
class Settings:
    min_band: int = 30
    ab_min_replies: int = 8
    ab_ratio: float = 2.0


# --- page properties -------------------------------------------------------------

def _select(props: dict, name: str) -> str:
    return (((props.get(name) or {}).get("select")) or {}).get("name", "") or ""


def _text(props: dict, name: str) -> str:
    return "".join(t.get("plain_text", "") for t in (props.get(name) or {}).get("rich_text", []))


def _date(props: dict, name: str) -> str:
    return (((props.get(name) or {}).get("date")) or {}).get("start", "") or ""


def normalize_channel(name: str) -> str:
    name = name.strip()
    return _CHANNEL_ALIASES.get(name.lower(), name) if name else ""


def sent_channel(communication: str) -> str:
    """Channel on the latest SENT line (by date; the later line wins a tie), "" if none."""
    latest: tuple[str, int, str] | None = None
    for i, line in enumerate(communication.splitlines()):
        m = _SENT_LINE.match(line)
        if m and (latest is None or (m.group(1), i) >= latest[:2]):
            latest = (m.group(1), i, m.group(2))
    return normalize_channel(latest[2]) if latest else ""


def page_state(props: dict) -> str | None:
    """sent / skipped / pending / None (left out). See the module docstring."""
    status = _select(props, "Status")
    communication = _text(props, "Communication")
    hard_signal = bool(
        _date(props, "Last Contacted")
        or any(_SENT_LINE.match(line) for line in communication.splitlines())
        or _select(props, "Outcome")
    )
    if hard_signal or status in _SENT_STATUSES:
        return "sent"
    if _select(props, "Verdict") == "Skip":
        return "skipped"
    if status == "Dead":
        return None  # Dead alone: no proof a message went out
    if _text(props, "Draft Message").strip() or status in _PENDING_STATUSES:
        return "pending"
    return None


def page_row(page: dict, lead=None) -> PageRow | None:
    """A page (and its latest JSONL lead, if any) -> PageRow; None if left out.
    Tier / Market / Industry / Lead Type come from the page (what you saw when you
    decided), the JSONL fills gaps; confidence is JSONL only."""
    props = page.get("properties", {})
    state = page_state(props)
    if state is None:
        return None
    outcome = _select(props, "Outcome")
    status = _select(props, "Status")
    communication = _text(props, "Communication")

    def pick(page_value: str, lead_attr: str) -> str:
        return page_value or (getattr(lead, lead_attr, None) or "") or UNKNOWN

    row = PageRow(
        page_id=page.get("id", ""),
        state=state,
        tier=pick(_select(props, "Tier"), "tier"),
        lead_type=pick(_select(props, "Lead Type"), "lead_type"),
        market=pick(_select(props, "Market").strip().lower(), "market"),
        niche=pick(_select(props, "Industry"), "niche"),
        confidence=(getattr(lead, "score_confidence", None) or UNKNOWN),
        campaign=_select(props, "Campaign"),
    )
    if state == "skipped":
        row.skip_reason = _select(props, "Skip Reason") or "(none)"
    if state == "sent":
        channel = sent_channel(communication)
        if not channel:
            channel = normalize_channel(_select(props, "Suggested Channel"))
            row.channel_fallback = True
        row.channel = channel or UNKNOWN
        row.replied = (
            outcome in _REPLY_OUTCOMES
            or status in _REPLY_STATUSES
            or any(_REPLY_LINE.search(line) for line in communication.splitlines())
        )
        row.meeting = outcome in _MEETING_OUTCOMES
    return row


def keep_campaign(row: PageRow, campaign: str, include_legacy: bool, legacy: str) -> bool:
    """Cohort filter: campaign keeps only that Campaign (the legacy one too, when
    named); otherwise the legacy cohort is left out unless include_legacy."""
    if campaign:
        return row.campaign == campaign
    return include_legacy or row.campaign != legacy


# --- tables -----------------------------------------------------------------------

@dataclass
class Group:
    value: str
    sent: int = 0
    skipped: int = 0
    pending: int = 0
    replied: int = 0
    meetings: int = 0

    @property
    def decided(self) -> int:
        return self.sent + self.skipped

    @property
    def send_rate(self) -> float | None:
        return self.sent / self.decided if self.decided else None

    @property
    def skip_rate(self) -> float | None:
        return self.skipped / self.decided if self.decided else None

    @property
    def reply_rate(self) -> float | None:
        return self.replied / self.sent if self.sent else None

    @property
    def meeting_rate(self) -> float | None:
        return self.meetings / self.sent if self.sent else None


def group_by(rows: list[PageRow], dim: str) -> list[Group]:
    """Groups for one dimension (tier, market, …): tiers A–D first, the rest by size."""
    groups: dict[str, Group] = {}
    for row in rows:
        g = groups.setdefault(getattr(row, dim), Group(getattr(row, dim)))
        if row.state == "sent":
            g.sent += 1
            g.replied += row.replied
            g.meetings += row.meeting
        elif row.state == "skipped":
            g.skipped += 1
        else:
            g.pending += 1

    def order(g: Group):
        if dim == "tier" and g.value in TIERS:
            return (0, TIERS.index(g.value), "")
        return (1, -(g.decided + g.pending), g.value)

    return sorted(groups.values(), key=order)


def skip_reason_counts(rows: list[PageRow]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.state == "skipped":
            counts[row.skip_reason] = counts.get(row.skip_reason, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def tier_trend(groups: list[Group], min_band: int) -> str:
    """Healthy = skip-rate rises A → C (PIPELINE.md §7). Judged only on tiers A–C
    with at least min_band decided pages, and only with two or more of them."""
    usable = [g for g in groups if g.value in TIERS[:3] and g.decided >= min_band]
    if len(usable) < 2:
        return f"not enough data: needs 2+ of tiers A–C with {min_band}+ decided pages each"
    rates = [g.skip_rate for g in usable]
    path = " → ".join(f"{g.value} {_pct(g.skip_rate)}" for g in usable)
    small = [g.value for g in groups if g.value in TIERS[:3] and 0 < g.decided < min_band]
    if small:
        path += f"; {', '.join(small)} under {min_band}, left out"
    if all(a <= b for a, b in zip(rates, rates[1:])):
        return f"healthy: skip-rate rises with tier ({path})"
    return (f"skip-rate doesn't rise with tier ({path}): a sub-score weight may be wrong, "
            "see SCORING.md §11")


def ab_rule(groups: list[Group], settings: Settings) -> str:
    """strategy_gaps.md S19: an arm wins with ≥ ab_min_replies replies and about
    ab_ratio× the other arm's reply rate. The best arm by reply rate is compared
    with the runner-up. Either arm under min_band sends = not enough data."""
    arms = [g for g in groups if g.sent > 0]
    if not arms:
        return "no sends yet"
    if len(arms) < 2:
        return f"only one arm with sends ({arms[0].value}); nothing to compare"
    arms.sort(key=lambda g: (-g.reply_rate, -g.replied, g.value))
    lead, runner = arms[0], arms[1]
    vs = (f"{lead.value} {_pct(lead.reply_rate)} ({lead.replied}/{lead.sent}) vs "
          f"{runner.value} {_pct(runner.reply_rate)} ({runner.replied}/{runner.sent})")
    if lead.replied < settings.ab_min_replies:
        return f"not enough data: {vs}; the leader needs {settings.ab_min_replies}+ replies"
    small = [g.value for g in (lead, runner) if g.sent < settings.min_band]
    if small:
        return f"not enough data: {vs}; under {settings.min_band} sends: {', '.join(small)}"
    if runner.reply_rate == 0 or lead.reply_rate >= settings.ab_ratio * runner.reply_rate:
        return f"{lead.value} wins: {vs}"
    return f"no clear difference: {vs} (a win needs ~{settings.ab_ratio:g}× the other arm)"


# --- text output ------------------------------------------------------------------

def _pct(rate: float | None) -> str:
    return "—" if rate is None else f"{round(rate * 100)}%"


def _table(header: list[str], body: list[list[str]]) -> list[str]:
    widths = [max(len(str(r[i])) for r in [header, *body]) for i in range(len(header))]
    line = lambda r: "  " + "  ".join(  # noqa: E731
        str(c).ljust(w) if i == 0 else str(c).rjust(w) for i, (c, w) in enumerate(zip(r, widths))
    ).rstrip()
    return [line(header), *(line(r) for r in body)]


_DIMS_L2 = (("tier", "Tier"), ("lead_type", "Lead type"), ("market", "Market"),
            ("niche", "Niche"), ("confidence", "Confidence"))
_DIMS_L3 = (("tier", "Tier"), ("market", "Market"), ("niche", "Niche"), ("channel", "Channel"))


def format_report(rows: list[PageRow], settings: Settings, left_out: int = 0) -> str:
    sent = [r for r in rows if r.state == "sent"]
    counts = {s: sum(1 for r in rows if r.state == s) for s in ("sent", "skipped", "pending")}
    small = f"* = under {settings.min_band}, not enough data"
    out = [
        f"score-report: {counts['sent']} sent · {counts['skipped']} skipped · "
        f"{counts['pending']} pending (drafted, no decision)"
        + (f" · {left_out} pages left out (not drafted, or Dead with no sent signal)" if left_out else ""),
    ]

    out += ["", f"L1 vs L2 — send / skip by group (rates over sent + skipped; {small})"]
    for dim, title in _DIMS_L2:
        groups = group_by(rows, dim)
        out += ["", f" {title}"]
        out += _table(
            [title.lower(), "sent", "skipped", "pending", "send-rate", "skip-rate", ""],
            [[g.value, g.sent, g.skipped, g.pending, _pct(g.send_rate), _pct(g.skip_rate),
              "*" if g.decided < settings.min_band else ""] for g in groups],
        )
        if dim == "tier":
            out.append(f"  → {tier_trend(groups, settings.min_band)}")

    reasons = skip_reason_counts(rows)
    out += ["", "Skip Reason counts (a reason that keeps recurring is a missing gate)"]
    out += _table(["reason", "pages"], [[r, n] for r, n in reasons]) if reasons else ["  no skips yet"]

    fallback = sum(1 for r in sent if r.channel_fallback)
    out += ["", f"L1 vs L3 — replies by group (rates over sent; {small})",
            f"  channel = latest SENT line in Communication; {fallback} of {len(sent)} sent pages "
            "had no SENT line and used Suggested Channel"]
    for dim, title in _DIMS_L3:
        groups = [g for g in group_by(rows, dim) if g.sent]
        out += ["", f" {title}"]
        if not groups:
            out.append("  no sends yet")
            continue
        out += _table(
            [title.lower(), "sent", "replied", "reply-rate", "meetings", "meeting-rate", ""],
            [[g.value, g.sent, g.replied, _pct(g.reply_rate), g.meetings, _pct(g.meeting_rate),
              "*" if g.sent < settings.min_band else ""] for g in groups],
        )

    out += ["", f"A/B rule (≥ {settings.ab_min_replies} replies and ~{settings.ab_ratio:g}× the other "
                f"arm's reply rate; {settings.min_band}+ sends per arm)"]
    for dim, title in (("market", "Markets"), ("niche", "Niches")):
        out.append(f"  {title}: {ab_rule(group_by(rows, dim), settings)}")
    return "\n".join(out) + "\n"
