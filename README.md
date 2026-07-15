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

Not yet implemented: enrichment (Phase 2), scoring (Phase 3), Notion sync (Phase 4).

## Setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
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

# tests
.venv/bin/python -m pytest
```

- `--categories` takes friendly names (full list in `pipeline/collectors/osm.py`)
  or raw OSM tags like `"craft=roofer"`
- `--location` must match the OSM area name; local spelling and English both work
- `--limit N` keeps only the first N leads — use it before spending rate-limit
  budget on a full batch
- Coming with later phases: `enrich`, `score`, `sync`
