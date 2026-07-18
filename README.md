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
| `enrich --run <run_id>` | Contact scrape, then Lighthouse audit |
| `score --run <run_id>` | Apply scoring weights from `config.yaml` |
| `sync --run <run_id> --sink notion` | Push scored leads to Notion |
| `run` | Chains all four stages |

Every command accepts `--limit N` — use it as a dry run before spending
rate-limit budget on a full batch.

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
