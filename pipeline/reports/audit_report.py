"""One-page audit report per lead (v2 step 8, strategy_gaps.md S16).

The "one-page note with concrete suggestions" (at most 3) that the audit-offer first
message promises. Built from the shared audit core's fields on a Lead (stored in the run,
or fresh via `audit-report --rescan`), in the lead's market language. Pure: no I/O, the
caller passes the date, config and wording.

The findings are the scorer's own Need items (`need_items`), ranked by their Need points
(config scoring.need.redesign, with the niche's need_overrides), so the report and the
score read a site the same way. A certificate error is the one finding of its own.

Markdown is the editable source (with a placeholder for a manual finding); the HTML is
rendered from it, so edits to the .md reach the HTML on the next run.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date

from pipeline.reports import texts_en, texts_sr
from pipeline.schema import Lead
from pipeline.scoring.scorer import audit_case, need_items

TEXTS = {"sr": texts_sr, "en": texts_en}
MAX_FINDINGS = 3
MANUAL_PLACEHOLDER = "<!-- add your own finding -->"
BRAND = "ONIX Web Lab"
_FAST_PERF = 90  # Lighthouse performance from which "loads quickly" is a positive

# audit_case -> skip reason (no report for these)
_SKIP_CASES = {
    "no_website": "no website",
    "dead_site": "domain dead",
    "probe_failed": "site didn't load",
}


@dataclass(frozen=True)
class Finding:
    item: str  # Need item, or "cert_error"
    text_key: str  # key in the texts' FINDINGS ("outdated_cms" for a legacy builder)
    params: dict = field(default_factory=dict)


def skip_reason(lead: Lead) -> str | None:
    """Why this lead gets no report (before findings are looked at), or None."""
    case = audit_case(lead)
    if case in _SKIP_CASES:
        return _SKIP_CASES[case]
    if case == "no_audit" and lead.site_reachable is None:
        return "not audited (old run: enrich --only audit --rescan, or audit-report --rescan)"
    if lead.language not in TEXTS:
        return f"no report wording for language {lead.language or '(none)'}"
    return None


def findings(lead: Lead, need_cfg: dict, year: int, need_overrides: dict | None = None) -> list[Finding]:
    """At most MAX_FINDINGS, most important first. `need_cfg` is scoring.need. Items a niche
    turned off (0 points) are left out. Empty list: the site looks fine."""
    case = audit_case(lead)
    if case == "cert_error":
        return [Finding("cert_error", "cert_error", {"domain": lead.domain})]
    if case not in ("audited", "no_audit"):
        return []
    points = {**need_cfg["redesign"], **(need_overrides or {})}
    hits = [item for item, _ in need_items(lead, need_cfg, year, with_lighthouse=case == "audited")]
    ranked = sorted((h for h in hits if points.get(h, 0) > 0), key=lambda h: -points[h])  # stable on ties
    out = []
    for item in ranked[:MAX_FINDINGS]:
        if item == "outdated" and not (
            lead.copyright_year is not None and lead.copyright_year <= year - need_cfg["outdated_years"]
        ):
            out.append(Finding(item, "outdated_cms", {"cms": lead.cms}))
        elif item == "outdated":
            out.append(Finding(item, "outdated", {"year": lead.copyright_year}))
        else:
            out.append(Finding(item, item, {"domain": lead.domain}))
    return out


def positives(lead: Lead, year: int) -> list[str]:
    """What the site already does well (keys of the texts' WORKS), only from signals
    that are known to be true."""
    if audit_case(lead) not in ("audited", "no_audit"):
        return []
    mobile = lead.mobile_friendly if lead.mobile_friendly is not None else lead.has_viewport_meta
    perf = (lead.lighthouse or {}).get("performance")
    checks = (
        ("https", lead.https is True),
        ("mobile", mobile is True),
        ("fast", perf is not None and perf >= _FAST_PERF),
        ("click_to_call", lead.has_click_to_call is True),
        ("booking", lead.has_booking is True),
        ("messaging", lead.has_messaging_link is True),
        ("recent", lead.copyright_year is not None and lead.copyright_year >= year - 1),
    )
    return [key for key, ok in checks if ok]


def build_report(
    lead: Lead, found: list[Finding], works: list[str], checked: date, signature: str,
) -> str:
    """The report as Markdown, in the lead's language. `found` must not be empty."""
    if not found:
        raise ValueError("no findings: a report needs at least one (skip the lead instead)")
    t = TEXTS[lead.language]
    lines = [
        f"# {t.TITLE.format(company=lead.company)}",
        "",
        t.CHECKED.format(domain=lead.domain, date=t.date(checked)),
        "",
        t.INTRO.format(domain=lead.domain),
        "",
    ]
    for n, f in enumerate(found, 1):
        title, text = t.FINDINGS[f.text_key]
        lines += [f"{n}. **{title.format(**f.params)}**", f"   {text.format(**f.params)}", ""]
    lines += [MANUAL_PLACEHOLDER, ""]
    if works:
        lines += [f"## {t.WORKS_HEADING}", ""] + [f"- {t.WORKS[k]}" for k in works] + [""]
    lines += ["---", "", t.CLOSING, "", signature, ""]
    return "\n".join(lines)


# --- Markdown -> self-contained HTML -------------------------------------------------
# Only the subset build_report writes (plus what a hand edit is likely to add): # / ##
# headings, numbered and bulleted items with indented continuation lines, ---, paragraphs,
# **bold**. HTML comments (the manual-finding placeholder) are dropped.

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_OL_ITEM = re.compile(r"^\d+\.\s+")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str) -> str:
    return _BOLD.sub(r"<strong>\1</strong>", html.escape(text.strip(), quote=False))


def _list_items(lines: list[str], marker: re.Pattern) -> list[str]:
    items: list[list[str]] = []
    for line in lines:
        if marker.match(line):
            items.append([marker.sub("", line)])
        elif items:
            items[-1].append(line.strip())
    out = []
    for first, *rest in items:
        body = f"<span class=\"title\">{_inline(first)}</span>" if first.startswith("**") else _inline(first)
        if rest:
            body += f"<p>{_inline(' '.join(rest))}</p>"
        out.append(f"<li>{body}</li>")
    return out


def _blocks(md: str) -> list[str]:
    text = _COMMENT.sub("", md)
    chunks = [c.strip("\n") for c in re.split(r"\n\s*\n", text) if c.strip()]
    out: list[str] = []
    for chunk in chunks:
        lines = chunk.splitlines()
        first = lines[0].strip()
        if first.startswith("## "):
            out.append(f"<h2>{_inline(first[3:])}</h2>")
        elif first.startswith("# "):
            out.append(f"<h1>{_inline(first[2:])}</h1>")
        elif first == "---":
            out.append("<hr>")
        elif _OL_ITEM.match(first):
            items = "".join(_list_items(lines, _OL_ITEM))
            if out and out[-1].startswith("<ol>"):  # items separated by blank lines stay one list
                out[-1] = out[-1][: -len("</ol>")] + items + "</ol>"
            else:
                out.append(f"<ol>{items}</ol>")
        elif first.startswith("- "):
            out.append(f"<ul>{''.join(_list_items(lines, re.compile(r'^- ')))}</ul>")
        else:
            out.append(f"<p>{'<br>'.join(_inline(line) for line in lines)}</p>")
    return out


_CSS = """
@page { size: A4; margin: 18mm 20mm; }
* { box-sizing: border-box; }
body { margin: 0; background: #f4f4f2; color: #1d1d1f;
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 720px; margin: 32px auto; background: #fff; padding: 44px 52px; border-radius: 6px;
  box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.brand { font-size: 12px; letter-spacing: .14em; text-transform: uppercase; color: #6e6e73;
  border-bottom: 2px solid #1d1d1f; padding-bottom: 10px; margin-bottom: 28px; }
h1 { font-size: 24px; line-height: 1.25; margin: 0 0 4px; }
h1 + p { color: #6e6e73; font-size: 13px; margin-top: 0; }
h2 { font-size: 15px; margin: 28px 0 8px; }
ol { padding-left: 1.3em; margin: 12px 0; }
ol li { margin: 0 0 14px; padding-left: 4px; }
ol li .title { font-weight: 600; }
ol li p { margin: 2px 0 0; color: #3a3a3c; }
ul { padding-left: 1.2em; margin: 6px 0; color: #3a3a3c; }
hr { border: 0; border-top: 1px solid #d2d2d7; margin: 28px 0 18px; }
@media print {
  body { background: #fff; font-size: 11.5pt; }
  main { margin: 0; padding: 0; max-width: none; box-shadow: none; border-radius: 0; }
  li, h2 { break-inside: avoid; }
}
"""


def markdown_to_html(md: str, language: str) -> str:
    """A self-contained page (inline CSS, no external files) that prints to one A4 page."""
    body = "\n".join(_blocks(md))
    title_match = re.search(r"^# (.+)$", md, re.MULTILINE)
    title = html.escape(title_match.group(1).strip()) if title_match else BRAND
    return (
        f"<!DOCTYPE html>\n<html lang=\"{html.escape(language)}\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{title} · {BRAND}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n<main>\n"
        f"<div class=\"brand\">{BRAND}</div>\n{body}\n</main>\n</body>\n</html>\n"
    )
