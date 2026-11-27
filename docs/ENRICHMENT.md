# AGENCY lead enrichment

## 1. Overview

Two enrichers, run in a fixed order, each satisfying the `Enricher`
protocol (see ARCHITECTURE.md §7 for the abstract shape):

```python
class Enricher(Protocol):
    def enrich(self, lead: Lead) -> Lead: ...   # returns a new Lead, never mutates
```

`runner.py enrich --run <run_id>` reads `collected.jsonl`, and for each
lead not already `status: enriched`, runs `pipeline.enrichers.base
.apply_enrichers()` against the ordered step list `_ENRICH_STEPS =
("discovery", "contact", "audit")` — discovery first (a found site feeds the other two), then contact data, since knowing whether a lead
is reachable at all is cheaper to establish than a full website audit,
and the audit step's notes want to summarize both.

| Enricher | Module | What it adds |
| --- | --- | --- |
| `discovery` | `pipeline/enrichers/discovery.py` | for leads with no website: guesses domains from the company name (`name.rs/.com/.co.rs`, hyphenated too) and adopts one only if the page shows a known phone or the full company name in its title/headings; the lead then becomes a normal audited lead |
| `contact` | `pipeline/enrichers/contact.py` | emails, phone numbers, social links |
| `audit` | `pipeline/enrichers/audit.py` | HTTPS, CMS, Lighthouse scores, mobile-friendly |

## 2. Contact enricher

Fetches the homepage plus at most one contact/kontakt page and extracts
emails, phone numbers, and social links. Pure parsing functions
(`extract_emails`, `extract_phones`, `extract_social_links`,
`find_contact_url`) operate on HTML strings, so the whole extraction
logic is fixture-tested against `tests/fixtures/contact_page.html` with
no network involved.

**`robots.txt` is checked before every single fetch** — homepage and
contact page alike — via `RobotFileParser`, cached per-domain for the
life of the enricher instance (`_robots_for`). This is the CLAUDE.md rule
("Do not scrape a contact page without checking robots.txt first") in
its concrete form; a domain with no reachable `robots.txt` is treated as
allowing everything, and a domain that disallows the target path raises
`RobotsDisallowedError`, which is caught and recorded in `Lead.errors`
rather than treated as a network failure to retry.

Extraction rules, in order of what's tried first:
- **Emails**: `mailto:` links first (handles `?subject=` query strings and
  comma-separated addresses), then a regex over both raw HTML and
  rendered text (the latter catches entity-obfuscated addresses the raw
  source hides). Candidates are filtered against an asset-extension list
  (`logo@2x.png`-shaped strings match the email regex) and a domain
  blocklist (`example.com`, `sentry.io`, `wixpress.com`, etc. — page
  furniture, not real inboxes).
- **Phones**: `tel:` links first, then a regex over visible text only
  (raw HTML is full of phone-shaped tracking/script IDs). Candidates pass
  through `_plausible_phone` — 8 to 15 digits, starting with `+` or `0`,
  and rejected if they match a date-like pattern (`_DATE_LIKE_RE`), since
  page text often has a date sitting right next to a phone number and the
  two are easy to confuse by regex alone.
- **Social links**: matched against a fixed host table (`linkedin.com`,
  `instagram.com`, `facebook.com`, `x.com`/`twitter.com`), skipping bare
  network homepages and share-widget URLs (`/sharer`, `/share`,
  `/plugins/`, etc.) so a Facebook "share this page" button doesn't get
  mistaken for the business's own profile.
- **Contact page discovery**: the first link whose `href` or visible text
  contains `"contact"` or `"kontakt"`. The enricher only follows it if the
  resulting URL normalizes to the *same domain* as the lead — nav
  "Contact" links occasionally point at a third-party form or a different
  property, and following those would attribute someone else's contact
  info to this lead.

Pages are decoded with `audit.html_text()` (contact and the audit probe alike): without a charset in the
`Content-Type` header, the page's own `<meta charset>` decides, not requests' ISO-8859-1 fallback, which turned
"Isailović u Šapcu" into "IsailoviÄ\x87 u Å apcu".

### One polite visitor per site (`audit.SiteClient`)

Every GET to a lead's own site during enrich (contact, discovery, the audit probe) goes through one `SiteClient`
that `cmd_enrich` shares across the enrichers:
- **One homepage fetch:** contact takes the homepage from the audit probe (`SiteClient.probe`, cached per domain;
  robots.txt checked first, and again for the final URL), and the audit reuses it. A probe failure is cached too.
- **Same-site spacing:** `enrich.same_domain_delay_s` (default 1.5 s) between two requests to the same host
  (robots.txt, homepage, contact page) and before Lighthouse starts. `enrich.delay_s` still spaces different leads.
- **429:** wait `Retry-After` (seconds or an HTTP date, max 10 s; 10 s when missing) and retry once. A second 429
  is recorded like any HTTP error (`contact: …` / `probe_error: http_error`).

### `profile_facts` (schema v14)

The contact step also builds `profile_facts` with `pipeline/enrichers/profile.py` (pure, fixture-tested): short,
labelled facts the drafter may praise when there is no `SM Audit` note (script_edits.md §11.3). Sources, most
praise-worthy first: the Maps description (`maps_description`, gosom's `descriptions` column, kept at collect), the
homepage meta description, services named in h1–h3 headings, the page title, the Maps category. Generic headings
(kontakt, o nama, usluge, početna, contact, about, home, services, cookies/kolačići, menu/meni…), the bare company
name and duplicates are dropped, as are headings whose first part is generic ("O nama – Brand") or that open
with a pitch ("Zašto odabrati …", "Welcome to …"); at most 5 facts of ~80 characters each. No extra request: it uses the homepage
HTML already fetched. Leads without a website (or blocked by robots.txt) get the Maps facts only; the drafter rebuilds
those for leads enriched before v14.

Whatever the collector already found (`lead.contact_emails`,
`contact_phones`, `social_links`) is merged with what this step finds,
collector-provided values winning ties (`_merge_unique`: first occurrence
wins, by lowercased email / `phone_key` / dict key).

## 3. Audit enricher and the shared audit core

The audit itself lives in **`pipeline/audit.py`**, the shared audit core (v2 step 3). It works on a domain and
knows nothing about `Lead`, so the `audit` enricher, the `audit-report` command (docs/OUTREACH.md, step 8) and later the public
scanner all use the same code. `pipeline/enrichers/audit.py` is a thin wrapper: it calls `audit_site()` and maps
the `AuditResult` onto the lead (`apply_audit`).

It runs last, after contact enrichment, so its `audit_notes` summary can report on both.

**Probe and Lighthouse are separate.** A failure in one never costs the other:

1. **Probe** (`probe`): one plain `GET`, https tried before http. `https` on the `Lead` means "the `https://` URL
   itself worked". A dead site costs one request, not a Lighthouse run.
   - If both schemes fail: `site_reachable = False` and `probe_error` holds the failure kind: `dns`, `refused`,
     `timeout`, `http_error`, `ssl` or `other`. Only `dns` / `refused` mean nothing is there. The others may be a
     live site worth checking again (scorer v4 treats them that way). When https and http fail differently, the
     more alive-looking kind wins (a cert error on https plus a refusal on http is `ssl`, not dead).
   - From the probe HTML, with no extra request: `https`, `cms` and the **signals** below.
2. **Lighthouse** (`run_lighthouse`), only for a site that answered. One attempt, no retry. A failure (timeout,
   crash, binary missing) is recorded in `Lead.errors` as `audit: …` and the lead keeps everything the probe
   found, with `site_reachable = True` and `lighthouse = None`.

Neither case raises, so `apply_enrichers` never retries the audit and the lead ends up `enriched`, not
`enrich_failed`. Re-runs therefore don't repeat Lighthouse on bad sites (this replaces the plan's G3 retry cap).
The enrich manifest counts both: `site_unreachable`, `lighthouse_failed`.

**Signals** (`extract_signals(html, current_year)`, pure, fixture-tested):

| Field | How |
| --- | --- |
| `copyright_year` | highest year (≤ current year) within 80 chars of `©`, "copyright", "all rights reserved" or "sva prava zadržana" / "zadrzana", in the visible text |
| `has_viewport_meta` | `<meta name="viewport">` present |
| `has_booking` | a known widget in the HTML (Calendly, Setmore, SimplyBook, Booksy, Fresha, Treatwell, Acuity, YouCanBookMe, Timify, Okadoc, Practo) or a date / datetime-local input |
| `has_click_to_call` | a `tel:` link |
| `has_messaging_link` | a `viber:` / `whatsapp:` link, or `wa.me` / `api.whatsapp.com` / `web.whatsapp.com` |
| `ads_tags` | `meta_pixel` (`fbevents.js`, `fbq(`), `google_ads` (`AW-…`, googleadservices), `gtm` (`gtm.js`, `GTM-…`). GA4 alone is not an ads tag |

All are `None` when not checked (no website, social page, or a run enriched before schema v11). The v3 scorer
ignores them; scorer v4 (step 5) uses them for Need and Viability. They don't reach the draft prompt yet.

**CMS detection** (`detect_cms`) checks HTML markers *before* the `<meta name="generator">` tag, deliberately:
page builders like Elementor overwrite the generator meta on a site that's still WordPress underneath, so the
marker table (`wp-content`, `cdn.shopify.com`, `wixstatic.com`, `website-files.com`, etc.) wins on a match. An
unrecognized generator string is still returned as-is.

**Lighthouse**: invoked as a subprocess (`npm install -g lighthouse`, see `docs/RESOURCES.md`), headless, JSON
to stdout, restricted to the four scored categories. `parse_lighthouse_report` reads
`report["categories"][key]["score"]`, which is nullable: a null score is omitted from `lead.lighthouse` rather
than coerced to 0, so scoring can tell "measured and bad" from "couldn't be measured". `mobile_friendly` comes
from Lighthouse's `viewport` audit.

**`build_audit_notes`**: one human-readable line combining contact-data counts (`"1 emails, 2 phones, 3
socials"`), the Lighthouse breakdown (`"LH perf 47 / a11y 88 / bp 68 / seo 92"`), HTTPS/mobile flags, and CMS.
Written at enrich time; scoring later *overwrites* this field (`docs/SCORING.md` §7). Unchanged in step 3.

A lead with no `domain`, or a Facebook/Instagram page as its domain, skips the audit; the enricher still writes
`audit_notes`.

### Re-auditing an enriched run: `enrich --only audit --rescan`

    python -m pipeline.runner enrich --run <run_id> --only audit --rescan [--limit N]

Runs only the audit again on the leads in `enriched.jsonl`, for runs enriched before schema v11 (no signals).
Contact data and `status` are kept; the audit fields are replaced. If Lighthouse fails this time, the previous
Lighthouse scores are kept (noted in `errors`). Leads without a site are left alone. `--limit N` re-audits the
first N only. Writes `enriched.jsonl` / `enriched.csv` and an `enrich_rescan` block in `manifest.json`. It
doesn't touch `scored.jsonl`, so run `score` again afterwards. `--only` and `--rescan` go together; `audit` is
the only step that can be re-run. A re-audit replaces the lead's earlier `audit:` error lines
(e.g. an old 429) instead of adding to them; other errors are kept.

## 4. Retry-once-then-fail, and degrade-don't-crash

`apply_enrichers()` in `pipeline/enrichers/base.py` is the orchestration
layer both enrichers run through. For each step, in order:

1. Call `enricher.enrich(current)`.
2. If it raises, sleep `retry_delay_s` (default 2s) and try exactly once
   more.
3. If the retry also raises, append `f"{name}: {retry_error}"` to
   `Lead.errors`, mark the lead as failed, and **move on to the next
   enricher anyway** — a broken contact fetch doesn't stop the audit step
   from still running on whatever data exists.

After all steps, the lead's `status` becomes `enrich_failed` if *any*
step exhausted its retry, or `enriched` if every step succeeded. This is
the concrete form of the CLAUDE.md "degrade, don't crash" principle: one
unreachable site never halts a 300-lead batch, and a partially-enriched
lead still carries forward whatever fields did succeed.

Enrichers themselves never raise `RobotsDisallowedError` through this
retry path as a *failure* to recover from — `contact.py` catches it
directly and returns the lead with the reason recorded in `errors`, since
retrying a `robots.txt` disallow a second time can't change the outcome.

## 5. Idempotent re-runs

Only leads already `status: enriched` are skipped on a re-run of `enrich
--run <run_id>` (ARCHITECTURE.md §9) — `enrich_failed` leads are retried,
since a transient timeout on the first pass shouldn't permanently exclude
a lead from ever getting contact data. This is why enrichment is safe to
re-run on an interrupted batch without hand-editing the JSONL first.

## 6. Adding a new enricher

1. Create `pipeline/enrichers/<name>.py`.
2. Write a class whose `enrich(self, lead: Lead) -> Lead` satisfies the
   `Enricher` protocol — return a **new** `Lead` via `dataclasses.replace`,
   never mutate the one passed in. Decorate it `@register("<name>")`.
3. Add it to `_ENRICH_STEPS` in `runner.py` in the position it should run.
4. Add a fixture-based test — `tests/test_contact_enricher.py` and
   `tests/test_audit_enricher.py` are the templates: pure parsing
   functions tested directly against saved HTML/JSON fixtures
   (`tests/fixtures/contact_page.html`, `tests/fixtures/lighthouse_sample.json`),
   with the class's own network/subprocess calls exercised separately
   using stub `requests`/`subprocess` responses, never live calls.
