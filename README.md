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
| `collect --source osm,maps,manual --market <id> --niche-file <yaml> --location "..."` | Collect + merge + dedupe leads (market and niche required) |
| `enrich --run <run_id>` | Find a website for leads listed without one (guessed from the name, verified on the page), then contact scrape, then Lighthouse audit |
| `score --run <run_id>` | Apply scoring weights from `config.yaml` |
| `sync --run <run_id> --sink notion [--min-tier C]` | Push qualified leads at tier ≥ `--min-tier` (default C) to Notion |
| `sync --sink notion --rescore [--run <run_id>]` | Update only the score properties on pages already in Notion (see [`docs/EXPORT.md`](docs/EXPORT.md) §3.4) |
| `draft-notion [--limit N] [--company NAME] [--redraft] [--dry-run] [--yes]` | Catch-up drafting for Notion pages with no draft yet (Verdict not Skip, Tier empty or ≥ B, Market and Industry set; `SM Audit` optional); writes when `outreach.auto_draft` is on, `--dry-run` previews. `--redraft` rewrites existing drafts |
| `run` | Chains collect, enrich, score, sync, then drafts this run's pages at Tier ≥ B automatically via the `claude` CLI (your Claude login, drafts only, never sends; never prompts) — see [`docs/OUTREACH.md`](docs/OUTREACH.md) |
| `go [--source ...] [--limit N] [--yes]` | Interactive: pick maps / osm / Notion drafts, answer the prompts, run (`--yes` skips the Start? confirm) |
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
prints `new per search term`, so you can see which search terms actually add leads. To
spread a big scrape out, put one location per entry in a batch (entries
also accept `bbox:` and `grid: false`).

### Manual leads

Two ways in:

```bash
# 1. from a CSV: copy leads_manual.csv.example to leads_manual.csv (gitignored), fill it in
.venv/bin/python -m pipeline.runner run --source manual --market serbia --niche-file niche.yaml \
  --input leads_manual.csv
.venv/bin/python -m pipeline.runner run --source maps,manual --market serbia --niche-file niche.yaml \
  --location "Beograd" --input leads_manual.csv   # combine with a scraper

# 2. pages you create by hand in Notion (Source = Manual, Market + Industry set, notes in SM Audit optional)
.venv/bin/python -m pipeline.runner draft-notion --limit 5 --dry-run   # prints drafts
.venv/bin/python -m pipeline.runner draft-notion                       # writes them to Notion
```

CSV columns are in [`docs/COLLECTION.md`](docs/COLLECTION.md). Drafting moves
Status from "Not started" to "Queued" (or "Draft failed" when the message fails
validation) and overwrites an existing draft only with `--redraft`.

### Interactive run

```bash
.venv/bin/python -m pipeline.runner go
```

First prompt picks what to run (skip it with `--source maps|osm|notion`):

- **Lead Gen**: asks
  1. the market: a preset (`serbia`, `uae`) or a new one, printed as YAML to paste into `config.yaml`
  2. the niche: typed in (name, Maps search terms, plural draft phrase, optional accepted Google
     categories / question / landing URL) or copied from an earlier run; nothing is preselected
  3. the source (maps / osm), plus OSM tags when the source is osm
  4. the location

  then collect, enrich, score, sync and draft (automatic, Tier ≥ B), with no further prompt
- **Draft Outreach**: asks which Notion pages (no draft yet / redraft existing) and how many (answer 1
  to pick one lead by name, default auto), then runs `draft-notion` (writes directly while
  `outreach.auto_draft` is on)

There are no default niches; the market presets come from `config.yaml`. Every Lead Gen run prints a summary and
asks "Start? (y/n)" first; `go --yes` skips it.

### Batches

List runs in `batches/<name>.yaml` (see `batches/example.yaml`; keys mirror
`run`: `source`, `market`, `niche`, `location`, `input`, `limit`, `sink`,
`include_seen`, `bbox`, `grid`). Every entry drafts after sync, like `run`.
`market` (a preset id or a full block with `id`) and a full `niche` block are required in every entry; every
entry is validated before the first one runs:

```yaml
runs:
  - source: maps
    market: serbia
    location: Beograd
    niche:
      name: auto detailing
      search_terms: [detailing, poliranje]
      phrase: auto-detailing studije
  - source: osm
    market: serbia
    location: Novi Sad
    limit: 20
    niche: {name: dental clinic, search_terms: [dentist], phrase: stomatološke ordinacije, osm_tags: [dentist]}
```

```bash
.venv/bin/python -m pipeline.runner batch --file batches/week1.yaml --dry-run
.venv/bin/python -m pipeline.runner batch --file batches/week1.yaml
```

- Entries run strictly one after another; a failed entry is recorded and the
  batch moves on. Finished entries are saved in `data/batches/<name>.json` and
  skipped on re-run.
- **Out of AI credits / usage limit:** drafting stops cleanly (finished drafts
  kept). The batch doesn't pause: later entries still collect, enrich, score and
  sync, but skip drafting, and the batch exits 3. The undrafted pages keep an
  empty Draft Message; run `draft-notion` once credits are back. A single `run`
  behaves the same (sync done, exit 3).

```bash
# collect, enrich, score, sync and draft in one pass, dry-run first
.venv/bin/python -m pipeline.runner run --source osm --market serbia --niche-file niche.yaml \
  --location "Novi Sad" --limit 15

# individual stages against an existing run
.venv/bin/python -m pipeline.runner enrich --run <run_id>
.venv/bin/python -m pipeline.runner score --run <run_id>
.venv/bin/python -m pipeline.runner sync --run <run_id> --sink notion --limit 2

# rebuild the local Notion dedupe cache from what's actually in Notion
.venv/bin/python -m pipeline.runner sync --sink notion --refresh-cache

# tests
.venv/bin/python -m pytest
```

- the niche's `osm_tags` take friendly names (full list in `pipeline/collectors/osm.py`)
  or raw OSM tags like `"craft=roofer"`; Maps uses its `search_terms`
- `--location` must match the OSM area name; local spelling and English both work
- `--limit N` keeps only the first N leads
