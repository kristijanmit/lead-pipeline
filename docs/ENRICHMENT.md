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
("contact", "audit")` — contact data first, since knowing whether a lead
is reachable at all is cheaper to establish than a full website audit,
and the audit step's notes want to summarize both.

| Enricher | Module | What it adds |
| --- | --- | --- |
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

Whatever the collector already found (`lead.contact_emails`,
`contact_phones`, `social_links`) is merged with what this step finds,
collector-provided values winning ties (`_merge_unique`: first occurrence
wins, by lowercased email / `phone_key` / dict key).

## 3. Audit enricher

Runs second, and only after contact enrichment, so its `audit_notes`
summary can report on both contact completeness and site quality
together.

**Cheap probe first**: `_probe` does one plain `GET` (https tried before
http) to confirm the site is reachable and capture the HTML — this costs
one request, versus letting a dead or slow site burn a 60-second-plus
Lighthouse attempt before failing. `https` on the `Lead` means "the
`https://` URL itself resolved," not merely "the domain has a cert
somewhere."

**CMS detection** (`detect_cms`) checks HTML markers *before* the
`<meta name="generator">` tag, deliberately — page builders like Elementor
overwrite the generator meta on a site that's still WordPress underneath,
so the marker table (`wp-content`, `cdn.shopify.com`, `wixstatic.com`,
`website-files.com`, etc.) is checked first and wins on a match. An
unrecognized generator string is still returned as-is rather than
discarded — partial data beats no data.

**Lighthouse**: invoked as a subprocess (`npm install -g lighthouse` —
see `docs/RESOURCES.md`), headless, JSON output to stdout, restricted to
the four categories the pipeline scores on
(`--only-categories=performance,accessibility,best-practices,seo`).
`parse_lighthouse_report` reads `report["categories"][key]["score"]`,
which is nullable (a category can error out mid-run) — a null score is
simply omitted from `lead.lighthouse` rather than coerced to 0, so
scoring (`docs/SCORING.md`) can tell "measured and bad" apart from
"couldn't be measured." `mobile_friendly` comes from Lighthouse's
`viewport` audit — a page with no viewport meta tag renders desktop-sized
under Lighthouse's default mobile emulation.

**`build_audit_notes`**: one human-readable line combining contact-data
counts (`"1 emails, 2 phones, 3 socials"`), the Lighthouse breakdown
(`"LH perf 47 / a11y 88 / bp 68 / seo 92"`), HTTPS/mobile flags, and CMS.
Written at enrich time; scoring later *overwrites* this same field with a
score-explainability string (`docs/SCORING.md` §7) — `audit_notes` means
something different before and after the `score` stage runs.

A lead with no `domain` skips the probe and Lighthouse entirely; the
enricher still writes `audit_notes` (via the "no website" branch) so
every lead gets a notes string regardless of whether it had a site to
audit.

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
