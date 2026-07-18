# AGENCY lead pipeline

Single-operator, zero-budget lead generation: collect businesses from free
sources → enrich with contact data and a website audit → score for outreach
priority → land in Notion. See `ARCHITECTURE.md` for the design and
`ROADMAP.md` for phase status.

## Implemented (Phase 1 — collection)

- Canonical `Lead` dataclass (`pipeline/schema.py`) + JSONL read/write
- Domain normalization and dedup (`pipeline/dedupe.py`) — one dedup key, computed in one place
- Three collectors behind a common `Collector` protocol (`pipeline/collectors/`):
  - **osm** — Overpass API; friendly category names or raw OSM `key=value` tags; matches local/English area names
  - **maps** — wraps `gosom/google-maps-scraper` (binary in `tools/`, runs via Rosetta); supplement only, ToS gray area
  - **manual** — hand-gathered leads from `leads_manual.csv` (template: `leads_manual.csv.example`)
- Multi-source runs merged and deduped in one pass (`--source osm,manual`)
- Per-run output under `data/runs/<run_id>/`: `collected.jsonl` (source of truth), `collected.csv` (human view), `manifest.json` (params, counts, timestamps)
- `config.yaml` defaults with CLI override, fail-fast validation
- Fixture-based test suite (no live network): `pytest`

## Implemented (Phase 2 — enrichment)

- Two enrichers behind a common `Enricher` protocol (`pipeline/enrichers/`),
  run in order by `enrich` — contact data first, audit second:
  - **contact** — emails, phone numbers (every one found, never collapsed to
    one), and LinkedIn/Instagram/Facebook/X links from the homepage plus one
    contact/kontakt page; `robots.txt` checked before every fetch
  - **audit** — local Lighthouse CLI run (performance, accessibility,
    best-practices, SEO scores + mobile-friendly), plus HTTPS and CMS
    detection from a cheap reachability probe
- Degrade, don't crash: each step retried once, then the failure lands in
  `Lead.errors` (`status: enrich_failed`) and the batch keeps going
- Idempotent re-runs: leads already `status: enriched` in `enriched.jsonl`
  are skipped, so an interrupted batch resumes where it stopped
- `audit_notes` summarizes contact-data completeness + all four Lighthouse
  categories per lead

## Implemented (Phase 3 — scoring)

- Pure scoring function (`pipeline/scoring/scorer.py`, no I/O): hard
  qualification filters (industry + country, `qualification:` in
  `config.yaml`) gate everything else; qualified leads get an independent
  `Reachability Score` and `Opportunity Score`; the reasoning behind every
  weight is in `SCORING.md`
- Each lead classified as `new_build` / `redesign` / `unclear`, ranked by
  `total_score = opportunity_score × reachability-derived multiplier` in
  `scored.jsonl`/`scored.csv`
- `Audit Notes` auto-generated on every scoring run, explaining exactly how
  each number was reached

## Implemented (Phase 4 — Notion sync)

- `NotionSink` (`pipeline/sinks/notion_sink.py`) pushes scored leads to the
  leads database via the Notion API; field mapping lives in one function,
  `to_notion_properties()` (ARCHITECTURE.md §6.1)
- Idempotent: a local `seen_domains.json` cache (never a live Notion query
  per lead) plus `status: synced` in `scored.jsonl`; re-runs and interrupted
  batches skip what already landed
- Per-lead API failures are reported and retried on the next run — one bad
  lead doesn't halt the batch

Not yet implemented: outreach handoff (Phase 5).

### Notion setup (one-time)

1. Create an internal integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Connect it to the leads database: database page → `•••` → Connections
3. Put the token in the gitignored `.env` at the repo root:
   `NOTION_TOKEN=ntn_...`

The target database id lives in `config.yaml` under `notion.database_id`.

## Setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install -g lighthouse   # website audit engine (Phase 2) — needs Chrome
```

Maps collector only: download a `darwin-amd64` release binary from
[gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper/releases)
to `tools/google-maps-scraper` and `chmod +x` it (path is set as
`maps.binary` in `config.yaml`). First run downloads a headless Chromium
(~550MB, cached in `~/Library/Caches/ms-playwright`).

## Commands

```bash
# collect from OSM — always dry-run with --limit first
.venv/bin/python -m pipeline.runner collect --source osm \
  --location "Novi Sad" --categories "dentist,hairdresser" --limit 15

# full batch, multiple sources, merged + deduped
.venv/bin/python -m pipeline.runner collect --source osm,maps,manual \
  --location "Novi Sad" --categories "dentist"

# manual leads only (CSV path defaults to leads_manual.csv)
.venv/bin/python -m pipeline.runner collect --source manual --input leads_manual.csv

# enrich a collected run — dry-run a few sites first, then run the rest;
# re-running skips leads that are already enriched
.venv/bin/python -m pipeline.runner enrich --run <run_id> --limit 3
.venv/bin/python -m pipeline.runner enrich --run <run_id>

# score an enriched run (weights from config.yaml)
.venv/bin/python -m pipeline.runner score --run <run_id>

# push scored leads to Notion — dry-run a couple of pages first
.venv/bin/python -m pipeline.runner sync --run <run_id> --sink notion --limit 2
.venv/bin/python -m pipeline.runner sync --run <run_id> --sink notion

# rebuild the local dedupe cache from what's actually in Notion
.venv/bin/python -m pipeline.runner sync --sink notion --refresh-cache

# tests
.venv/bin/python -m pytest
```

- `--categories` takes friendly names (full list in `pipeline/collectors/osm.py`)
  or raw OSM tags like `"craft=roofer"`
- `--location` must match the OSM area name; local spelling and English both work
- `--limit N` keeps only the first N leads — use it before spending rate-limit
  budget on a full batch
