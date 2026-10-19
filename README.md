# AGENCY lead pipeline

Single-operator, zero-budget lead generation: collect businesses from free
sources → enrich with contact data and a website audit → score for outreach
priority → land in Notion.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — data model, stage
  contracts, run lifecycle, dedup/idempotency
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — phase status
- [`docs/COLLECTION.md`](docs/COLLECTION.md) — OSM/Maps/manual collectors
- [`docs/ENRICHMENT.md`](docs/ENRICHMENT.md) — contact scraping + Lighthouse audit
- [`docs/SCORING.md`](docs/SCORING.md) — the scoring model, why every weight is what it is
- [`docs/OUTREACH.md`](docs/OUTREACH.md) — the draft stage: channel and message rules
- [`docs/EXPORT.md`](docs/EXPORT.md) — CSV + Notion sinks, sync idempotency
- [`docs/RESOURCES.md`](docs/RESOURCES.md) — external tools this project builds on

## Setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install -g lighthouse   # website audit engine — needs Chrome
```

Maps collector only (optional, supplement source — see docs/COLLECTION.md
§3): download a `darwin-amd64` release binary from
[gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper/releases)
to `tools/google-maps-scraper` and `chmod +x` it; path is `maps.binary` in
`config.yaml`. First run downloads a headless Chromium (~550MB, cached in
`~/Library/Caches/ms-playwright`).

### Notion setup (one-time, for `sync`)

1. Create an internal integration at
   [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Connect it to the leads database: database page → `•••` → Connections
3. Copy the database id out of its URL, then put both secrets in the
   gitignored `.env` at the repo root:
   ```
   NOTION_TOKEN=ntn_...
   NOTION_DATABASE_ID=...
   ```

`config.yaml`'s `notion.database_id` stays empty — both secrets live in
`.env` only, never in a committed file.

## Commands

All stages run through `pipeline.runner`:

| Command | Does |
| --- | --- |
| `collect --source osm,maps,manual --location "..." --categories "..."` | Collect + merge + dedupe leads |
| `enrich --run <run_id>` | Find a website for leads listed without one (guessed from the name, verified on the page), then contact scrape, then Lighthouse audit |
| `score --run <run_id>` | Apply scoring weights from `config.yaml` |
| `draft --run <run_id>` | Suggest a channel and draft one Serbian first message per qualified lead via the `claude` CLI (your Claude login, drafts only, never sends) — see [`docs/OUTREACH.md`](docs/OUTREACH.md) |
| `sync --run <run_id> --sink notion` | Push scored leads to Notion (Status Queued, Suggested Channel, Draft Message) |
| `draft-notion [--limit N] [--yes]` | Draft for hand-made Notion pages (`Source = Manual`, notes in `SM Audit`); dry run unless `--yes` |
| `run` | Chains collect, enrich, score, draft and sync |
| `go [--source ...] [--limit N]` | Interactive: pick maps / osm / Notion drafts, answer the prompts, run |
| `batch --file batches/<name>.yaml [--dry-run] [--pause-s N]` | Runs the listed `run` entries one after another (see below) |

Every command accepts `--limit N` — use it as a dry run before spending
rate-limit budget on a full batch.

`collect` skips leads already in the Notion cache (`--include-seen` keeps
them), so overlapping areas and queries aren't enriched or drafted twice.

### More leads from Maps: grid mode

The Maps scraper returns about 20 leads per query and can't scroll deeper (see
[`docs/COLLECTION.md`](docs/COLLECTION.md) §3). Instead, a location listed under
`maps.areas` in `config.yaml` (Beograd, Novi Sad, Niš) is split into grid cells and
each cell is searched, which gives several times more leads. It is automatic;
`--bbox "minLat,minLon,maxLat,maxLon"` targets any other area, `--no-grid` turns it
off. A grid run takes minutes (the cell count and timeout are printed), and collect
prints `new per category`, so you can see which categories actually add leads. To
spread a big scrape out, put one location or category per entry in a batch (entries
also accept `bbox:` and `grid: false`).

### Manual leads

Two ways in:

```bash
# 1. from a CSV: copy leads_manual.csv.example to leads_manual.csv (gitignored), fill it in
.venv/bin/python -m pipeline.runner run --source manual --input leads_manual.csv
.venv/bin/python -m pipeline.runner run --source maps,manual --location "Beograd" \
  --categories "detailing" --input leads_manual.csv   # combine with a scraper

# 2. pages you create by hand in Notion (Source = Manual, notes in SM Audit)
.venv/bin/python -m pipeline.runner draft-notion --limit 5   # dry run, prints drafts
.venv/bin/python -m pipeline.runner draft-notion --yes       # writes them to Notion
```

CSV columns are in [`docs/COLLECTION.md`](docs/COLLECTION.md). `draft-notion`
never overwrites an existing draft and only moves Status from "Not started"
to "Queued".

### Interactive run

```bash
.venv/bin/python -m pipeline.runner go
```

First prompt picks what to run (skip it with `--source`):

- `maps` / `osm`: asks for the scrape location and category/categories
  (comma-separated, e.g. `detailing, poliranje`), then runs the whole chain
- `notion`: dry-runs `draft-notion` for hand-made Notion pages, then asks
  before writing the drafts

Defaults come from `config.yaml`; every run asks "Start? (y/n)" first.

### Batches

List runs in `batches/<name>.yaml` (see `batches/example.yaml`; keys mirror
`run`: `source`, `location`, `country`, `input`, `categories`, `limit`, `sink`,
`include_seen`, `bbox`, `grid`, plus `no_draft: true` to stop after score):

```yaml
runs:
  - source: maps
    location: Beograd
    categories: detailing
  - source: maps
    location: Novi Sad
    categories: "detailing,poliranje"
    limit: 20
```

```bash
.venv/bin/python -m pipeline.runner batch --file batches/week1.yaml --dry-run
.venv/bin/python -m pipeline.runner batch --file batches/week1.yaml
```

- Entries run strictly one after another; a failed entry is recorded and the
  batch moves on. Finished entries are saved in `data/batches/<name>.json` and
  skipped on re-run.
- **Out of AI credits / usage limit:** drafting stops cleanly (exit code 3,
  finished drafts kept, nothing synced without its draft) and the batch pauses.
  Re-run the same command once credits are back; it resumes that entry at
  draft, then continues. For a single run, resume with `draft --run <run_id>`
  then `sync`.

```bash
# collect, enrich, score, and sync in one pass, dry-run first
.venv/bin/python -m pipeline.runner run --source osm \
  --location "Novi Sad" --categories "dentist,hairdresser" --limit 15

# individual stages against an existing run
.venv/bin/python -m pipeline.runner enrich --run <run_id>
.venv/bin/python -m pipeline.runner score --run <run_id>
.venv/bin/python -m pipeline.runner sync --run <run_id> --sink notion --limit 2

# rebuild the local Notion dedupe cache from what's actually in Notion
.venv/bin/python -m pipeline.runner sync --sink notion --refresh-cache

# tests
.venv/bin/python -m pytest
```

- `--categories` takes friendly names (full list in `pipeline/collectors/osm.py`)
  or raw OSM tags like `"craft=roofer"`
- `--location` must match the OSM area name; local spelling and English both work
- `--limit N` keeps only the first N leads
