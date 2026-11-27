# Lead pipeline — the complete sketch

One page, end to end: how a business on a map becomes a reviewed Serbian first message in Notion, and
what comes after (follow-ups, replies, retuning). Each stage links to its deep-dive doc. Built stages are
marked **[built]**, planned ones **[planned]** (status lives in docs/ROADMAP.md).

```
 SOURCES                 RUN FOLDER: data/runs/<run_id>/            NOTION (outreach tracker)          YOU
 ───────                 ──────────────────────────────            ─────────────────────────          ───
 OSM (Overpass) ─┐
 Google Maps    ─┼─► 1 COLLECT ─► collected.jsonl
 Manual CSV     ─┘        │ dedupe (domain) + skip seen_domains.json
                          ▼
                     2 ENRICH ──► enriched.jsonl
                          │ discovery → contact → audit (Lighthouse)
                          ▼
                     3 SCORE ───► scored.jsonl + scored.csv
                          │ qualify → reachability × opportunity
                          ▼
                     4 SYNC ─────────────────────────────────────► page per lead ──────────► read, add
                          │ seen_domains.json cache                  Status = Not started       `SM Audit` note
                          ▼                                                 │                         │
                     manifest.json (counts, config, timestamps)             ▼                         ▼
                                                                  5 DRAFT-NOTION ◄────────────────────┘
                                                                     Suggested Channel, Draft Message,
                                                                     Status = Queued ────────────────► review, edit,
                                                                                                       SEND BY HAND,
                                                                                                       log in
                                                                                                       `Communication`
                                                                  6 FOLLOW-UP [planned] ◄─────────────┘
                                                                     Follow-up due / Dead
                                                                  7 FEEDBACK [planned]
                                                                     replies → retune scoring + prompt
```

Nothing is ever sent by the pipeline. Every outward message is a human action.

---

## 0. Entry points

| Command | What it chains | Use when |
| --- | --- | --- |
| `go [--yes]` | interactive menu: **Lead Gen** (1→5; asks market, niche, source, location), **Draft Outreach** (5 as catch-up) | day-to-day |
| `run --source … --market … --niche-file … --location …` | 1 → 2 → 3 → 4 → 5, never prompts | one scripted run |
| `batch --file batches/<name>.yaml` | `run` per entry, sequential, `--pause-s` between | several cities / niches |
| individual stages (`collect`, `enrich`, `score`, `sync`, `draft-notion`) | one stage | debugging, re-running a step |

- Every command takes `--limit N`: dry-run on a few leads before spending rate limits or AI credits.
- `run` builds the sink first, so a missing `NOTION_TOKEN` fails before collect/enrich spend time.
- `batch` state is in `data/batches/<name>.json`. One failed entry doesn't stop the rest; running out of
  AI credits (exit code 3) pauses the batch, and a re-run resumes that entry at the draft step.

## 1. Collect **[built]** — docs/COLLECTION.md

`collect --source osm,maps,manual --market serbia --niche-file niche.yaml --location "Novi Sad"` (or `go`)

Every run has a **market** (preset in `config.yaml` `markets:`, or created in `go`) and a **niche** (created in
`go` or given in full; no defaults). Maps searches the niche's `search_terms`, OSM its `osm_tags`. See
docs/COLLECTION.md §1.1.

| Source | Module | Notes |
| --- | --- | --- |
| OSM / Overpass | `collectors/osm.py` | primary; free; **never parallel queries** (shared infrastructure) |
| Google Maps | `collectors/maps.py` (gosom scraper binary) | supplement only (ToS gray area). Depth >1 is broken, so a location in `maps.areas` (or `--bbox`) runs in **grid mode**: area split into `grid_cell_km` cells, ~20 results each, ~3× leads. `--no-grid` = one plain query |
| Manual CSV | `collectors/manual.py` | `leads_manual.csv`, hand-gathered |

Then, in order:
1. Stamp `location` (unless the collector set it), and `country`, `market`, `language`, `industry` = `niche`,
   `niche_label` from the run's market and niche; both blocks go into `manifest.json`.
2. **Dedupe** by normalized domain (`pipeline/dedupe.py` — no scheme, no `www.`, lowercase; `sites.google.com/view/<x>` keeps its path; the only place
   this key is computed).
3. **Skip leads already in Notion** via `data/seen_domains.json`, and leads the L0 gates rejected in an earlier
   run via `data/rejected_domains.json` (`--include-seen` keeps both). Each kept lead gets `listing_count`.
4. `--limit N` keeps the first N.

Output: `collected.jsonl` (`status: collected`), manifest counts (raw, after dedupe, already in Notion,
new per category). Lead fields filled: identity (`company`, `domain`, `source`, `industry`) and listing
details (`address`, `rating`, `review_count`, `profile_url` — Maps gives all, OSM address + object page).

## 2. Enrich **[built]** — docs/ENRICHMENT.md

`enrich --run <run_id>` — runs three enrichers in fixed order on every lead not yet `status: enriched`
(idempotent re-runs). Each returns a new `Lead`; failures go to `Lead.errors`, never halt the batch.
`enrich.delay_s` pause between sites.

| Step | Module | Adds |
| --- | --- | --- |
| `discovery` | `enrichers/discovery.py` | no website? guess `name.rs/.com/.co.rs` (hyphenated too); adopt only if the page shows a known phone or the full company name. The lead then continues as a normal site lead |
| `contact` | `enrichers/contact.py` | homepage + at most one contact page, **robots.txt checked before every fetch**. `contact_emails` (all of them, never collapsed), `contact_phones`, `social_links`, `site_text` (short homepage summary for drafting), `profile_facts` (≤ 5 praise-able facts from the homepage + Maps description/category, `enrichers/profile.py`) |
| `audit` | `enrichers/audit.py` over the shared core `pipeline/audit.py` | probe first: `site_reachable` / `probe_error`, HTTPS, CMS and signals from the homepage HTML (`copyright_year`, `has_viewport_meta`, `has_booking`, `has_click_to_call`, `has_messaging_link`, `ads_tags`); then Lighthouse CLI scores and `mobile_friendly`, one attempt, a failure keeps the probe fields. `audit_notes`. `enrich --only audit --rescan` re-audits an enriched run |

Output: `enriched.jsonl` (`enriched` or `enrich_failed`). Retry once, then fail into data.

## 3. Score **[built]** — docs/SCORING.md

`score --run <run_id>` — `scoring/scorer.py`, a pure function (no I/O, no config loading inside).

1. **Qualify** (filter, not score): `country` equals the run's market country (from the manifest). Industry is deliberately
   neither filtered nor weighted yet.
2. **Reachability** (0–100): email 50 + phone 40 + social 10.
3. **Opportunity** + `lead_type`, from one branch so they never disagree:
   | Case | `lead_type` | `opportunity_score` |
   | --- | --- | --- |
   | no domain | `new_build` | 90 (fixed) |
   | domain, Lighthouse ok | `redesign` | 100 − weighted Lighthouse (perf .40, bp .25, a11y .20, seo .15) + 10 no HTTPS + 10 not mobile, capped 85 |
   | domain, no usable audit | `unclear` | none — needs a manual look |
4. **Total** = opportunity × multiplier (reach 100 → 1.0, 90 → 0.95, 40–89 → 0.7, 1–39 → 0.5, 0 → 0.2).
5. `qualified` = passed the filter **and** reachability > 0.

Output: `scored.jsonl` + `scored.csv` (human view, never internal format). Bump `scoring_version` when the
formula changes.

## 4. Sync to Notion **[built]** — docs/EXPORT.md, docs/ARCHITECTURE.md §6.1

`sync --run <run_id> --sink notion` — creates one page per `scored` lead, ~3 req/s (`notion.delay_s`).

- Dedupe against the local `seen_domains.json` cache (domain, plus email match), **never a live Notion
  query per lead**. `sync --refresh-cache` rebuilds the cache from Notion.
- Field mapping lives only in `to_notion_properties()`: first email/phone into typed columns, the rest as
  text; `Social Links` as clickable links; Lighthouse flattened to `Label: value` lines; checkboxes only
  when known; scores only when set; `Qualified` always. Bookkeeping (`run_id`, `errors`, versions) stays in
  JSONL.
- Synced leads flip to `status: synced`. Existing pages are not updated (cache hit = skip).

## 5. Draft first messages **[built, automatic]** — docs/OUTREACH.md

No human gate: drafting runs right after sync in `run` / `batch` / `go` (script_edits.md §11).

- **Which pages**: this run's synced pages that are still draftable: `Qualified` ✓, Status `Not started` (blank
  counts), empty `Draft Message`, `Verdict` ≠ `Skip`, `Tier` ≥ `outreach.auto_draft_min_tier` (B), Market and
  Industry set. `SM Audit` optional. Drafted from the run's JSONL lead (niche_label, profile_facts, niche question).
- **Channel** (`pipeline/channels.py`, pure): the market's order and first-touch exclusions. No usable contact →
  no draft (`no_contact_data`).
- **Message** (`drafting/drafter.py`, one `claude -p` call, `draft.model`, ~1k tokens): the market language's
  rule set; praise from SM Audit → `profile_facts` → the plain line; one easy question last. Voice examples from
  `config/outreach.{language}.md`, filtered to the lead's channel and niche.
- **Validate** (`validate_message`): one retry with the problem named, then Status `Draft failed` + reason in
  `Draft Notes`.
- **Write** (`update_page`): `Suggested Channel`, `Draft Message`, `Draft Notes`, `Status = Queued`.
- A CLI / Notion error on one page goes to `Draft Notes` and the run continues. Out of AI credits → drafting stops,
  "N pages undrafted", exit 3 after sync; `draft-notion` picks them up later.

Then **you** send by hand (edit if needed) or set `Verdict = Skip` + `Skip Reason`, and log:
`Communication`: `YYYY-MM-DD · SENT · channel · text` / `YYYY-MM-DD · REPLY · text`, plus `Last Contacted`.
`draft-notion` is the catch-up: undrafted pages, hand-made pages, `--redraft`.

## 6. Follow-ups **[planned — Phase B]**

A `followup` stage in `pipeline/drafting/` (or `pipeline/followup/`), Notion-first like `draft-notion`.
Worth building only once real messages are logged.

- Reads: `Status`, `Last Contacted`, `Communication`, `Follow-up date`.
- For a page with a SENT entry and no REPLY after N days: write a follow-up draft, set `Follow-up date`,
  Status → `Follow-up due`.
- On the next run, still no reply after the follow-up: Status → `Dead`.
- Allowed writes only: Status (Queued / Follow-up due / Dead), Suggested Channel, Draft Message,
  Follow-up date. Never `Communication`, `Last Contacted`, `Chosen Channel`, `Notes`.
- Same `--dry-run`, `--limit` and out-of-credits behaviour as `draft-notion`.

## 7. Replies and feedback loop **[planned / idea]**

- **Reply drafts** (idea): classify the latest REPLY into send-me-more / "we have a site" / no budget /
  price, draft an answer. Needs approved price, reference and call slots in `config.yaml` `outreach`.
- **Prompt feedback**: edits you make to drafts in Notion → new approved examples in `config/outreach.{language}.md`.
- **Scoring retune** (Phase 3/6): after the first real batch, compare reply rate by `lead_type`, channel,
  industry and score band; adjust weights per docs/SCORING.md §7, bump `scoring_version`.

---

## Data and state at a glance

| Where | What | Owner |
| --- | --- | --- |
| `data/runs/<run_id>/*.jsonl` | the `Lead` records per stage — source of truth | pipeline |
| `data/runs/<run_id>/manifest.json` | config snapshot, counts, timestamps, errors — the only observability | pipeline |
| `data/seen_domains.json` | which leads are already in Notion | sync / collect |
| `data/batches/<name>.json` | batch progress (done / failed / paused + run id) | batch |
| Notion database | the working list: notes, drafts, status, communication log | you + draft stages |
| `config.yaml` | sources, grid areas, enrich timeouts, scoring weights, outreach settings | you |
| `config/outreach.{language}.md` | approved example messages by channel and niche | you |
| `.env` | `NOTION_TOKEN`, `NOTION_DATABASE_ID` | you (never committed) |

`Lead.status` lifecycle: `collected` → `enriched` | `enrich_failed` → `scored` → `synced`.
Notion `Status`: `Not started` → `Queued` → (sent by you) → `Follow-up due` → `Dead` [planned], or a reply.

## Principles that hold across every stage

- One `Lead` dataclass (`pipeline/schema.py`, schema v12) flows everywhere; new fields bump `schema_version`.
- Degrade, don't crash: missing data is a status + `Lead.errors`, not an exception.
- Idempotent re-runs: enrich skips enriched, sync and collect skip cached, drafting skips drafted.
- New sources / enrichers / sinks implement the protocol in their `base.py` and `@register` — `runner.py`
  doesn't change.
- Zero budget, single operator: no queues, databases or schedulers until ARCHITECTURE.md §14 says so.
