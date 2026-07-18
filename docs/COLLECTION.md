# AGENCY lead collection

## 1. Overview

Three collectors, one protocol, one job: turn a location + a set of
categories into a list of `Lead` records. `pipeline/collectors/base.py`
defines the contract (see ARCHITECTURE.md §7 for the abstract shape;
this doc is the concrete "how each one actually works" reference):

```python
class Collector(Protocol):
    def collect(self, params: dict) -> Iterable[Lead]: ...
```

`runner.py collect --source osm,maps,manual --location "..." --categories "..."`
resolves each requested source through the registry (`get_collector`),
calls `collect()` on it, then merges and dedupes the combined output by
domain (ARCHITECTURE.md §9) before writing `collected.jsonl`.

| Collector | Role | Module |
| --- | --- | --- |
| `osm` | Primary source | `pipeline/collectors/osm.py` |
| `maps` | Supplement, used sparingly | `pipeline/collectors/maps.py` |
| `manual` | Hand-gathered leads | `pipeline/collectors/manual.py` |

## 2. OSM collector (primary)

Wraps the Overpass API — free, no key, no quota, but shared public
infrastructure with a shared rate limit.

**Category resolution** (`resolve_category`): friendly names like
`"dentist"` or `"hairdresser"` map to an OSM `key=value` tag pair via the
`CATEGORY_TAGS` table (~30 entries covering food/drink, health, retail,
trades, and professional services). Anything not in that table can be
passed straight through as a raw tag on the CLI, e.g. `--categories
"craft=roofer"` — `resolve_category` treats any string containing `=` as
already-resolved.

**Query building** (`build_query`): the Overpass QL query matches the
location against three area-name tags (`name`, `name:en`, `int_name`) so
the CLI accepts either the local-language spelling ("Нови Сад") or the
English one ("Novi Sad"). Every requested category becomes an `nwr[...]`
clause inside that search area.

**Parsing** (`parse_elements`): unnamed OSM elements are skipped (nothing
to call the lead), but a missing `website` tag is not — it becomes an
empty `domain`, which is data, not an error. Emails and phone numbers
found across `email`/`contact:email` and `phone`/`contact:phone` tags are
deduped (case-insensitive for emails, by `phone_key` for phones) but never
collapsed to one.

**Rate limiting and retries**: `_fetch` retries up to `_MAX_ATTEMPTS = 3`
times with a linear backoff (`_BACKOFF_S * attempt` seconds) on any
request or JSON-parsing failure, then raises. There is no concurrency
here by design — **never parallelize Overpass queries**. It's shared
public infrastructure with a shared rate limit; a single-threaded,
backed-off client is the whole mitigation strategy (CLAUDE.md, "Don't
do"). This is why the collector has no worker-pool or async path even
though a naive implementation might reach for one.

## 3. Maps collector (supplement)

Wraps the external [`gosom/google-maps-scraper`](https://github.com/gosom/google-maps-scraper)
binary (see `docs/RESOURCES.md`) — not called directly, spawned as a
subprocess. **Supplement only, never the primary source**: scraping
Google Maps sits in a ToS gray area (CLAUDE.md, "Don't do"; ROADMAP.md
risks). Reach for it only where OSM coverage is thin for a given
category/area.

Mechanics:
- `build_queries` writes one line per category to a temp file, each
  suffixed `#!#<category>` — that custom ID comes back in the results CSV's
  `input_id` column, which is how `parse_results_csv` recovers which
  category each row came from without re-guessing it from the business
  name.
- The binary is invoked with `-c 1` (concurrency pinned to 1, mirroring
  the "sparingly" constraint on Overpass) and `-exit-on-inactivity 3m`.
  `-email` is passed when `maps.extract_emails` is true (default).
- Emails are cleaned with `_split_emails` — the scraper occasionally
  yields a bracket-and-quote-wrapped list, or an email with a stray
  leading `/` from a `"//foo@bar.com"` JS-comment artifact in raw page
  source; both are stripped before validation.
- **Known environment quirk**: the scraper's bundled Playwright-Go driver
  points at a retired CDN (`playwright.azureedge.net` /
  `cdn.playwright.dev`) for its first-run browser download, which some
  networks reject. The collector sets
  `PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright`
  in the subprocess environment as a working mirror (verified 2026-07) —
  if a future Playwright-Go release changes its download layout, this is
  the first place to check.
- `maps.depth` (default 3) and `maps.timeout_s` (default 900) are the
  knobs to reach for if a run times out — lower `depth` or split the
  category list across runs rather than raising the timeout indefinitely.

## 4. Manual collector

Reads `leads_manual.csv` (a gitignored working file — copy
`leads_manual.csv.example` to create it, or pass `--input <path>`).
Required column: `company`. Known columns: `website`, `industry`,
`location`, `emails` (`;`-separated), `phone` (also `;`-separated, same
"keep every number" rule as the automated collectors). Blank/partial rows
are silently skipped — common in a hand-edited CSV. An empty `location`
in the CSV is filled in from the run's `--location` by `cmd_collect`, not
by the collector itself.

This is the intended path for LinkedIn/Clutch/referral leads found by
hand — they flow through the same dedupe/enrich/score pipeline as
everything else instead of living in a separate spreadsheet.

## 5. Merging and dedup across sources

`--source osm,maps,manual` runs each requested collector in sequence
(never concurrently — see §2) and combines the results before
`collected.jsonl` is written. Duplicates are dropped by the normalized
domain, computed once in `pipeline/dedupe.py` — see ARCHITECTURE.md §9
for the general dedup/idempotency principle. This doc's only concern is
that it happens *before* enrichment: there's no reason to scrape a
contact page or run Lighthouse against the same domain twice because two
sources both found it.

## 6. Adding a new collector

1. Create `pipeline/collectors/<name>.py`.
2. Write a class whose `collect(self, params: dict) -> Iterable[Lead]`
   satisfies the `Collector` protocol, and decorate it `@register("<name>")`.
3. `runner.py` does not change — `get_collector("<name>", config)` finds it
   through the registry the moment the module is imported.
4. Add a fixture-based test (no live network calls — see CLAUDE.md
   Testing). `tests/test_osm_collector.py` is the template: a saved
   Overpass JSON response in `tests/fixtures/`, fed through the pure
   parsing function (`parse_elements`), asserting the resulting `Lead`
   fields. `tests/test_maps_collector.py` and `tests/test_manual_collector.py`
   follow the same shape against `tests/fixtures/maps_sample.csv`.

## 7. What collection does *not* do

Contact-page scraping and its `robots.txt` check happen in the
enrichment stage (`pipeline/enrichers/contact.py`), not here — see
`docs/ENRICHMENT.md`. Collectors only read structured data sources
(Overpass tags, the Maps scraper's own output, a hand-written CSV); they
never fetch a lead's website.
