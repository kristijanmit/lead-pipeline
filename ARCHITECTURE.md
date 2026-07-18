# AGENCY lead pipeline — architecture

## 1. Overview

A single-operator, zero-budget lead generation pipeline: collect businesses
from free sources, enrich them with contact data and a website audit, score
them for outreach priority, and land the result in Notion. Today this is a
handful of flat scripts (`collect_osm.py`, `enrich.py`, `score.py`). This
document defines the target architecture to grow into as ROADMAP.md phases
land — not a rewrite to do in one sitting.

## 2. Goals and non-goals

**Goals**

- Swap or add a data source, enrichment step, or output sink without touching
  the others (Phase 2 added a second collector; Phase 5/6 will add new sinks
  and signals — the architecture should absorb that as config, not surgery).
- Survive partial failure. One unreachable website or one bad HTTP response
  should not kill a batch of 300 leads.
- Be inspectable after the fact — when a score looks wrong, it should be
  possible to trace it back to the raw enrichment data that produced it.
- Stay operable by one person with a terminal and Claude Code. No servers to
  babysit, no databases to administer.

**Non-goals**

- Not building a distributed job queue, a web UI, or a multi-tenant system.
  This is a single operator's tool. If it outgrows that, see §10.
- Not abstracting for hypothetical future data sources that aren't on the
  roadmap. Two collectors existing is a pattern; one collector plus
  speculation is over-engineering.

## 3. Guiding principles

1. **A stage is a function of a list of leads, not a script with side effects
   scattered through it.** Collection, enrichment, scoring, and output are
   four distinct responsibilities. Each should be callable and testable on
   its own, independent of the CLI that drives it.
2. **One canonical schema, enforced at the boundary.** Every stage reads and
   writes the same `Lead` shape. A collector doesn't need to know what
   scoring does with `website_audit`; it just needs to produce a valid `Lead`.
3. **Degrade, don't crash.** A missing email, an unreachable site, a failed
   Lighthouse run — these are data, not exceptions that halt the batch.
4. **Idempotent by domain.** Re-running collection on the same city twice
   should not create duplicate leads downstream. Normalize domain as the
   dedup key as early as possible.
5. **Every run is reproducible.** A run's config, inputs, and outputs are
   snapshotted together, so "why did this lead score 62 three weeks ago" is
   answerable without guessing.

## 4. System diagram

```
                    ┌─────────────┐
 config.yaml  ───▶  │   Runner    │  ◀─── CLI args (location, categories)
 CLI args           │ (runner.py) │
                    └──────┬──────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   ┌─────────┐       ┌─────────┐        ┌─────────┐
   │ OSM     │       │ Maps    │        │ Manual  │      collectors/
   │collector│       │collector│        │  CSV    │      (pluggable)
   └────┬────┘       └────┬────┘        └────┬────┘
        └──────────────────┼──────────────────┘
                           ▼
                  merge + dedupe by domain
                           │
                           ▼
                 data/runs/<id>/collected.jsonl
                           │
                           ▼
              ┌────────────────────────┐
              │   Enrichment pipeline   │            enrichers/
              │ 1. contact (email/phone/│            (ordered,
              │    social)              │             pluggable)
              │ 2. website audit        │
              │    (Lighthouse)         │
              └────────────┬────────────┘
                           ▼
                  data/runs/<id>/enriched.jsonl
                           │
                           ▼
                      scoring/scorer.py
                    (pure function, no I/O)
                           │
                           ▼
                  data/runs/<id>/scored.jsonl
                           │
                ┌──────────┴──────────┐
                ▼                     ▼
          sinks/csv_sink.py     sinks/notion_sink.py
          (always runs,          (Phase 4 — dedupe
           human-readable         against existing
           export)                Notion pages)
```

## 5. Data model

A single canonical record flows through every stage. Defined once in
`pipeline/schema.py`, not re-invented per script.

```python
@dataclass
class Lead:
    schema_version: int = 6        # bump whenever fields are added/changed

    # identity — set at collection, never changes after
    company: str
    domain: str                    # normalized: no scheme, no "www.", lowercase
    source: str                    # "osm" | "maps" | "manual"
    industry: str = "other"
    location: str = ""             # the market the lead was collected in, e.g. "Novi Sad" (v5)
    country: str = ""              # v6 — the qualification filter's location gate checks this

    # contact enrichment (Phase 2, step 1)
    contact_emails: list[str] = field(default_factory=list)   # info@, sales@, etc — don't collapse to one
    contact_phones: list[str] = field(default_factory=list)   # same rule as emails — keep every number (v3)
    social_links: dict[str, str] = field(default_factory=dict)  # {"linkedin": "...", ...}

    # website audit (Phase 2, step 2)
    https: bool | None = None
    mobile_friendly: bool | None = None
    cms: str | None = None
    lighthouse: dict[str, int] | None = None   # {"performance": 42, "accessibility": 88, ...}
    audit_notes: str = ""          # overwritten with the scoring explainability string once scored (SCORING.md §5)

    # scoring (v6 — see SCORING.md for the full model)
    scoring_version: int = 3       # bump whenever the weighting formula changes
    reachability_score: float | None = None
    opportunity_score: float | None = None
    total_score: float | None = None
    lead_type: str | None = None   # "new_build" | "redesign" | "unclear" (v4) —
                                   # set by scorer.py, see SCORING.md §8
    qualified: bool = False        # industry + country filters passed AND reachability_score > 0

    # pipeline bookkeeping
    run_id: str = ""
    status: str = "collected"      # collected | enriched | enrich_failed | scored | synced
    errors: list[str] = field(default_factory=list)
```

**Two schema decisions worth calling out:**

- `contact_emails` is a list, not a single field. Real sites expose `info@`
  and `sales@` on the same page — collapsing to one email means the enricher
  silently picks a winner and throws data away. Same reasoning as
  `social_links` already being a dict.
- `contact_phones` (schema v3) follows the same rule — an office landline and
  a mobile number on the contact page are both worth keeping. Duplicate
  spellings of one number ("021/452-333" vs "+381 21 452 333") are collapsed
  by a comparison key (`dedupe.phone_key`), never by rewriting the stored
  formatting. v2 JSONL with a single `contact_phone` string still loads —
  `Lead.from_dict` migrates it into the list.
- `status` is an explicit small enum, not just "did it work or not." A site
  that 403'd, one that timed out, and one with simply no email found are
  different situations — a future retry policy or manual-review queue needs
  to tell them apart, not just see `errors: [...]` and shrug.
- `schema_version` and `scoring_version` travel with every record. Without
  them, a `scored.jsonl` from three weeks ago (produced by an older scoring
  formula) looks identical to today's output but isn't comparable — you'd
  have no way to know a score of 62 last month meant something different
  than 62 today.

Why JSONL instead of CSV as the internal format: `social_links` and `errors`
are naturally nested, and the schema will keep growing (Phase 6 adds signals
nobody's thought of yet). CSV forces everything flat and breaks quietly when
a column is missing. JSONL keeps each stage's output self-describing.
**CSV is still produced as a sink** — for pasting into Sheets/Notion review —
but it is a view of the data, not the source of truth for the pipeline.

## 6. Module structure

```
pipeline/
  schema.py          # Lead dataclass + validation
  config.py           # load + validate config.yaml, fail fast on bad config
  dedupe.py           # domain normalization + seen-domain tracking

  collectors/
    base.py           # Collector protocol: collect(params) -> Iterable[Lead]
    osm.py             # wraps Overpass API
    maps.py            # wraps gosom/google-maps-scraper
    manual.py          # reads leads_manual.csv

  enrichers/
    base.py           # Enricher protocol: enrich(lead: Lead) -> Lead
    contact.py         # email/phone/social scraping
    audit.py           # Lighthouse CLI wrapper

  scoring/
    scorer.py          # score(lead: Lead, weights: dict, qualification: dict) -> Lead — pure function

  sinks/
    base.py           # Sink protocol: write(leads: list[Lead]) -> None
    csv_sink.py         # always-on human-readable export
    notion_sink.py       # Phase 4 — direct API push with dedupe; see §6.1
                         # for the Lead -> Notion property mapping rules

  runner.py           # CLI entrypoint; wires the above together

data/
  runs/<run_id>/
    manifest.json       # config snapshot, counts, timestamps, errors
    collected.jsonl
    enriched.jsonl
    scored.jsonl
    scored.csv          # human-readable export

tests/
  test_scoring.py       # pure function — no mocks needed
  test_dedupe.py
  test_schema.py
  fixtures/             # sample HTML pages, sample Overpass responses
```

### 6.1 Notion sink field mapping

Notion's property types don't have a native list or map type, so `notion_sink.py`
has to flatten a few `Lead` fields deliberately rather than fail on them:

| `Lead` field                                            | Notion property                 | Mapping rule                                                                                                                                                |
| ------------------------------------------------------- | ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ | ------------------------------- | ------------------------------------------------------------------- |
| `contact_emails[0]`                                     | `Primary Email` (Email)         | First address, since Notion's Email type validates a single string                                                                                          |
| `contact_emails[1:]`                                    | `Additional Emails` (Rich text) | Joined with `, ` — informational only, not clickable as mailto                                                                                              |
| `contact_phones[0]`                                     | `Phone` (Phone number)          | Same rule as emails: first number, since Notion's Phone type is a single string                                                                             |
| `contact_phones[1:]`                                    | `Additional Phones` (Rich text) | Joined with `, ` — same rule as additional emails                                                                                                           |
| `social_links`                                          | `Social Links` (Rich text)      | Rendered as Markdown links, one per line: `[LinkedIn](url)`                                                                                                 |
| `https`, `mobile_friendly`                              | two Checkbox properties         | Only sent when not `None` — a checkbox can't say "unknown", so omit instead of asserting false                                                              |
| `lead_type`                                             | `Lead Type` (Select)            | `new_build` / `redesign` / `unclear` — the outreach step filters on it                                                                                      |
| `source`                                                | `Source` (Select)               | `osm`/`maps` → "OSM/Maps", `manual` → "Manual"; `industry`, `location`, and `cms` go out raw and Notion auto-creates select options                         |
| `lighthouse["performance"                               | "accessibility"                 | "seo"                                                                                                                                                       | "best_practices"]` | four separate Number properties | Flattened one key per column — Notion has no nested-object property |
| `reachability_score`                                    | `Reachability Score` (Number)   | Only sent when not `None` (a lead that failed the qualification filters gets no score at all — SCORING.md §1)                                              |
| `opportunity_score`                                     | `Opportunity Score` (Number)    | Only sent when not `None` — an `unclear` lead_type leaves this blank rather than guessing which formula applies (SCORING.md §3)                            |
| `qualified`                                              | `Qualified` (Checkbox)          | Always sent — industry + country filters passed AND `reachability_score > 0` (SCORING.md §1-2)                                                             |
| `schema_version`, `scoring_version`, `run_id`, `errors` | _(none)_                        | Deliberately not synced — Notion is a sink for humans to act on, not the source of truth; this bookkeeping stays in the JSONL manifest where it's queryable |

This mapping lives in one function (`to_notion_properties(lead: Lead) -> dict`)
in `notion_sink.py` — if the `Lead` schema grows again, this is the one place
that needs a matching update, not every call site.

## 7. Stage interfaces

Three small protocols are the entire contract. Nothing else needs to know
about HTTP, HTML parsing, or the Overpass query language.

```python
class Collector(Protocol):
    def collect(self, params: dict) -> Iterable[Lead]: ...

class Enricher(Protocol):
    def enrich(self, lead: Lead) -> Lead: ...   # returns a new Lead, doesn't mutate in place

class Sink(Protocol):
    def write(self, leads: list[Lead]) -> None: ...
```

Adding `collect_linkedin.py` later means writing one class that satisfies
`Collector` and registering it — `runner.py` doesn't change. Same for a
future paid-provider enricher if AGENCY's budget changes: it's a new class
next to `contact.py`, not a rewrite.

## 8. Run lifecycle

1. `runner.py collect --source osm --location "..." --categories "..."`
   writes `data/runs/<run_id>/collected.jsonl` and a `manifest.json` with the
   params used, source, and raw count.
2. `runner.py enrich --run <run_id>` reads `collected.jsonl`, runs each
   enricher in order (contact first, audit second — per Phase 2), writes
   `enriched.jsonl`. Per-lead failures are caught, logged into that lead's
   `errors` list with a matching `enrich_failed` status, and the lead still
   moves forward with whatever fields did succeed — one broken site doesn't
   stop the batch.
3. `runner.py score --run <run_id>` loads `config.yaml` weights, applies the
   pure scoring function, writes `scored.jsonl` and `scored.csv`.
4. `runner.py sync --run <run_id> --sink notion` reads `scored.jsonl`,
   checks each domain against a local cache of what's already in Notion,
   and only creates pages for new ones; successfully pushed leads flip to
   `status: synced` in `scored.jsonl`.

Every command accepts `--limit N` to run against just the first N leads —
a first-class dry-run, not something improvised by hand-editing a CSV before
committing free-tier rate limit budget to a full batch.

Each command is independently re-runnable. If enrichment dies halfway through
a 300-lead run, `enrich.py` re-run doesn't need to hit already-succeeded
leads twice — see dedupe/idempotency below.

## 9. Deduplication and idempotency

- **Dedup key**: normalized domain (strip protocol, strip `www.`, lowercase,
  strip trailing slash). Computed once, in `dedupe.py`, used everywhere.
- **At collection**: merging multiple sources (OSM + Maps + manual) drops
  duplicates by this key before anything downstream sees them — no point
  enriching the same domain twice.
- **At sync**: before creating a Notion page, check the domain against a
  local `seen_domains.json` cache (refreshed periodically from Notion, not
  queried live on every single lead). Querying the full Notion database for
  a domain match on every sync works fine at a few dozen rows but gets slow
  and rate-limit-prone once the database has a few hundred — a periodically
  refreshed local cache is cheap to build now and avoids that cliff later.
- **Within a run**: if `enrich.py` is re-run on a `collected.jsonl` that
  already has a partial `enriched.jsonl`, only leads without a `status:
enriched` are reprocessed. Free API calls and scraped bandwidth aren't
  wasted redoing work that already succeeded.

## 10. Configuration

`config.yaml` holds defaults; CLI args override them at runtime (per Phase 1
— location/categories are given when the script starts, not buried only in
config). `config.py` validates the merged result once, at startup, and fails
with a clear message — not three stages later as a cryptic `KeyError`.

Secrets (a future Notion token, a future paid API key) go in a
`.env`-style file that's gitignored; `config.yaml` itself stays safe to
commit since AGENCY's own weights and ICP definitions aren't secret.

## 11. Error handling and resilience

- Network calls (Overpass, site fetches, Lighthouse) get a timeout, a small
  retry with backoff, and a hard cap — no request hangs a batch indefinitely.
- Every enrichment failure is caught at the per-lead level, recorded in
  `Lead.errors`, and logged — the batch continues.
- Rate limits are respected explicitly: a fixed delay between requests in
  `contact.py` and `audit.py`, and Overpass queries stay single-threaded
  (shared public infrastructure, not something to hammer).
- `robots.txt` is checked before scraping any contact page, same as today.

## 12. Observability

Each run writes `manifest.json`:

```json
{
  "run_id": "2026-07-20T09-15",
  "stage": "enrich",
  "config_snapshot": { "...": "..." },
  "counts": { "input": 240, "enriched": 231, "failed": 9 },
  "started_at": "...",
  "finished_at": "..."
}
```

This is what makes "why did this batch only produce 40 leads" a two-minute
question instead of re-running everything with print statements.

## 13. Testing strategy

- **Scoring is a pure function** — test it with hand-built `Lead` objects and
  known weights, no mocking required. This is the highest-value test in the
  whole project since a scoring bug silently misprioritizes every lead.
- **Dedup logic** — table of domain variants in, one canonical key out.
- **Collectors/enrichers** — test against saved fixture responses (a sample
  Overpass JSON blob, a sample HTML page), not live network calls. Live
  network is for manual runs, not the test suite.

## 14. When to graduate off this architecture

This is scripts-plus-JSONL on purpose — it's the right amount of structure
for one operator. Signals that it's time for something heavier (n8n,
Postgres, a scheduler):

- Running collection/enrich/score by hand more than once a week and wanting
  it scheduled unattended.
- Wanting multiple people to work leads at once (concurrent writes need a
  real datastore, not JSONL files).
- Adding a fifth or sixth collector/enricher and the CLI wiring in
  `runner.py` starts feeling like the bottleneck rather than the config.

None of these are true today — building for them now would be solving
problems AGENCY doesn't have yet.
