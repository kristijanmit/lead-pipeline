# Project Instructions

This is the AGENCY lead pipeline: a single-operator, zero-budget lead generation
tool. Collect businesses from free sources → enrich with contact data and a
website audit → score for outreach priority → land in Notion.

## Current state

The repo is docs-only right now (ARCHITECTURE.md, ROADMAP.md). Code lands
phase by phase per ROADMAP.md — build each phase into the target module
structure directly (`pipeline/collectors/`, `pipeline/enrichers/`, etc.),
not as standalone scripts to refactor later.

## Stack

- Python 3.12+ (dataclasses, `Protocol` interfaces), no framework
- JSONL files under `data/runs/<run_id>/` as the internal data store — no database
- Lighthouse CLI (local `npm install -g lighthouse`) for website audits — not the PageSpeed API
- Overpass API (OSM), gosom/google-maps-scraper, and manual CSV as collectors
- Notion API as the output sink (Phase 4)
- `config.yaml` for defaults; CLI args override at runtime; secrets in a gitignored `.env`

## Commands

All stages run through one CLI entrypoint (`pipeline/runner.py`):

- `python -m pipeline.runner collect --source osm --location "..." --categories "..."` — collect leads
- `python -m pipeline.runner enrich --run <run_id>` — contact enrichment, then website audit
- `python -m pipeline.runner score --run <run_id>` — apply scoring weights from config.yaml
- `python -m pipeline.runner sync --run <run_id> --sink notion` — push to Notion (Phase 4)
- Every command accepts `--limit N` — use it as a dry run before spending rate-limit budget on a full batch
- `pytest` — run tests

## Architecture

- Four stages — collect, enrich, score, sink — each callable and testable on
  its own. A stage is a function of a list of leads, not a script with side
  effects scattered through it.
- One canonical `Lead` dataclass in `pipeline/schema.py` flows through every
  stage. Never re-invent the record shape per script. Bump `schema_version`
  when fields change; bump `scoring_version` when the weighting formula changes.
- New collectors/enrichers/sinks implement the small protocols in each
  package's `base.py` (`Collector`, `Enricher`, `Sink`) and get registered —
  `runner.py` does not change.
- Degrade, don't crash: a missing email, unreachable site, or failed
  Lighthouse run is data (`Lead.errors` + a `status` value), not an exception
  that halts the batch.
- Dedup key is the normalized domain (no scheme, no `www.`, lowercase),
  computed only in `pipeline/dedupe.py`. Re-runs must be idempotent: enrich
  skips leads already `status: enriched`; Notion sync checks a local
  `seen_domains.json` cache, never a live Notion query per lead.
- Scoring (`pipeline/scoring/scorer.py`) is a pure function with no I/O.
  Keep it that way.
- JSONL is the source of truth; CSV is a human-readable sink view, never the
  internal format.
- Every run writes a `manifest.json` (config snapshot, counts, timestamps) —
  keep it accurate; it's the only observability this project has.
- Enrichers return a new `Lead`; they don't mutate in place.

## Documentation

For deeper context, consult these before guessing:

- `ARCHITECTURE.md` — full data model, module structure, stage protocols, run lifecycle, dedup/idempotency rules
- `ARCHITECTURE.md` §6.1 — the exact `Lead` → Notion property mapping (emails, social links, Lighthouse flattening); keep it in one function, `to_notion_properties()`
- `ROADMAP.md` — the six phases, what lands where, and current checkbox status; update checkboxes as work ships
- `SCORING.md` — the "why" behind every scoring weight, the fit/opportunity split, the manual intent workflow, and how to retune after real reply data
- `config.yaml` — scoring weights and ICP definitions (safe to commit; secrets are not)

## Testing

- Scoring is the highest-value thing to test: hand-built `Lead` objects and
  known weights, no mocks. A scoring bug silently misprioritizes every lead.
- Dedup: table of domain variants in, one canonical key out.
- Collectors/enrichers: test against saved fixtures in `tests/fixtures/`
  (sample Overpass JSON, sample HTML pages) — never live network calls in
  the test suite.

## Don't do

- Do not run parallel/concurrent Overpass queries — it's shared public infrastructure with a shared rate limit.
- Do not scrape a contact page without checking `robots.txt` first.
- Do not use the Maps scraper as a primary source — ToS gray area, supplement only.
- Do not collapse `contact_emails` to a single email — keep every address found.
- Do not add I/O, network calls, or config loading inside `scorer.py`.
- Do not commit anything under `data/` or any `.env`/secrets file.
- Do not build for hypothetical scale (job queues, databases, web UI, schedulers) — ARCHITECTURE.md §14 lists the signals that would justify it; none are true yet.
