# script_edits.md — v1 → v2 (layered scoring), checked against the code

> **Updated 2026-11-26 with three decisions:** no default niches (always created in `go`), English and non-local markets
> including the UAE, and the agency lane removed. Section 9 holds the edits these decisions add. Where sections 1–8 mention
> industry Fit or `industry_fit`, section 9 overrides them.

Checked against `kristijanmit/lead-pipeline` @ `7dc42c6` (2026-11-26), ~6.3k lines. All **205 tests pass** at this commit.
Line numbers below are from that commit.

Legend: **FIX** = wrong or inconsistent now · **CHANGE** = existing code, new behaviour · **ADD** = new code ·
**REPLACE** = old logic swapped · **DELETE** = remove · **DECIDE** = needs your call before coding.

---

## 1. Where the first plan was wrong (corrected here)

| # | Plan said | Code says | Correction |
| --- | --- | --- | --- |
| P1 | New scoring = `version: 2` | `scoring.version` is already **3** (`config.yaml:64`, `schema.py:16`, `config.py` defaults) | v2 scoring ships as **version 4**. Schema 9 → 10 is right. |
| P2 | `seen_domains.json` has no `page_id` and needs a new format | It already stores `{key: {page_id, synced_at, snapshot}}`, keyed `domain:x` / `company:x` (`notion_sink.py:386-390`) | `--rescore` can use the existing cache. Only the "rejected" idea needs a home (see C3). |
| P3 | Put `state: rejected` entries into `seen_domains.json` | `sync` reads `entry["page_id"]` on every cache hit (`notion_sink.py:359`), and the email index runs over every entry | A rejected entry with no page would crash comment creation. Use a **separate `data/rejected_domains.json`**. |
| P4 | New enricher `signals.py` parses the HTML that `contact` fetched | `contact.py` doesn't keep HTML. `audit.py:_probe()` already GETs the homepage and holds `html` (`audit.py:142`) | Compute the signals **inside `audit.py`** from the probe HTML. No new enricher, no extra request. |
| P5 | New `Score Reasons` text replaces the explanation | `scorer._build_audit_notes()` already writes a plain-language explanation into `audit_notes`. It syncs to Notion `Audit Notes`, and **the draft prompt reads it** (`drafter.py:292`, `lead_from_page` `notion_sink.py:282`) | Split it in two. `audit_notes` keeps only facts for drafting. The priority/score sentence moves to a new `score_reasons` field (Notion `Score Reasons`), which drafting never sees. |
| P6 | Reach = Instagram 100 … landline 40 | `channel.py` also drops **placeholder** phones/emails and detects Serbian mobiles. The scorer counts placeholders as contacts and counts *any* social link (LinkedIn, TikTok…), while drafting uses only IG/FB | The shared ranking must *be* `channel.py`'s logic (same filters), not a copy of its order. |

## 2. Gaps the plan missed

**Enrich**
- **G1. A Lighthouse failure throws away HTTPS and CMS too.** `audit.enrich()` (`audit.py:142-151`) returns all
  fields together. If `_run_lighthouse` raises, `https`/`cms` (which the probe already found) are lost.
  `apply_enrichers` then **retries the whole step**, so that's probe + Lighthouse up to 2× (up to ~4 min per bad site).
  That makes `unclear` leads look emptier than they are.
- **G2. `unclear` mixes two different cases.**
  - The probe failed: the site is dead or parked. That's closer to `new_build`, maybe an abandoned domain.
  - The probe worked but Lighthouse failed: the site is live and we only lack scores. That's a `redesign`.

  The plan treated both the same. They need different branches.
- **G3. `enrich_failed` leads get re-enriched on every re-run** (`runner.py:208-212` skips only `enriched`), so dead
  sites burn Lighthouse time forever. It needs a cap, e.g. at most 2 enrich attempts recorded in `errors`.

**Collect**
- **G4. Maps `industry` comes from the search query, not from Google's category.** `parse_results_csv` reads
  `input_id` (`maps.py:124-125`) and ignores the `category` column, which exists in the CSV (see `tests/fixtures/maps_sample.csv`).
  A "dentist" search also returns dental labs, pharmacies and so on, all labelled `dentist`. Keep the real category
  (`maps_category`) and flag a query/category mismatch. That's a cheap L0 gate or a Fit penalty.
- **G5. Business status isn't in the fixture.** The `status` column (closed etc.) is assumed. **Verify against one
  real gosom CSV** before building the "closed" gate. If it's missing, drop that gate.

**Score / sync**
- **G6. Sync pushes every scored lead, including unqualified ones** (`runner.py:547`), such as a country-gate failure
  or `reach = 0`. That's where Notion clutter comes from. `--min-tier` fixes this, and it should also skip `qualified = False`.
- **G7. `--rescore` has no code path.** `cmd_sync` only touches `status == "scored"` leads (`runner.py:547`). Synced
  leads are never revisited. It needs its own loop over `synced` leads → cache lookup (key or email index) →
  `update_page()` (that method already exists, `notion_sink.py:463`).
- **G8. Pages without JSONL can't be rescored.** Hand-made pages (Source = Manual) and the 77 backfilled pages from
  early runs may have no `scored.jsonl` that matches. `--rescore` must report "no source data" for them, not fail.
- **G9. Config has two sources plus validation.** The scoring defaults also live in `config.py` (`DEFAULTS`, lines ~83-101).
  `_validate` (lines 229-264) requires `reachability` / `opportunity` and the `audited_score_cap < no_website_score`
  rule. A v4 config **fails to load** until all three are updated together.

**Drafting / Notion**
- **G10. Four code paths draft before review:** `run --draft`, `go` → "Both", batch `draft: true`, and the batch
  resume path (`runner.py:704`, which always runs `cmd_draft`). A Verdict gate in `draft-notion` alone doesn't cover them.
- **G11. Hand-made pages would be blocked.** `draft-notion` is also your flow for hand-made leads. Once drafting
  requires `Verdict = Go`, those pages need a Verdict too. The migration and docs must say so.
- **G12. Ratings and Lighthouse numbers already reach the prompt** through `audit_notes` (`_reputation_sentence`, the
  priority sentence). The prompt says "never mention them", and the validator only catches ratings. P5's split
  closes this.
- **G13. Notion has ~34 columns already** (ROADMAP ideas). Adding 8 more makes it worse. Put Fit/Need/Viability/Reach
  in one `Score Reasons` line, plus `Tier` and `Priority` as columns, and hide the old score columns.

**Plan vs your own docs**
- **G14. v2 reverses two recorded v3 decisions without saying so** (`SCORING.md` §2 and §5.2, changelog v3). See D1 and D2.
- **G15. Instagram-only businesses get the weakest data.** The roadmap's to-do #2 says many leads are beauty salons
  living on Instagram. In v2 they get Viability = 50 (no data) and Need from "has social" only. The Instagram bio/posts
  enrichment from the roadmap is what would give them real signals. It's listed as a follow-up, not part of v2.
- **G16. Calibration needs a join between Notion and JSONL.** `score-report` must read *all* runs' `scored.jsonl`, take the
  latest row per `cache_key`, and join to pages by the same key or email index. The plan didn't say how.
- **G17. `SCORING.md` §9 is already stale.** It lists tests for an industry filter and `dentist` → `clinic` folding
  that no longer exist. Rewrite it with v4, not on top.

## 3. Decisions needed before coding

| # | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| **D1** | ~~Industry Fit~~ **Decided:** no default niches. The niche is created per run in `go`, so there's no industry Fit. It's replaced by a niche-match check (section 9). | — | — |
| **D2** | ~~"No website always outranks a bad audited site"~~ **Decided 2026-11-26: dropped.** Remove `audited_score_cap < no_website_score` from `config.py` validation, the `assert` in the scorer and the config key. new_build and redesign compete on Need. Record the reason in the `SCORING.md` changelog (v4): with Instagram-only salons, "no site" isn't automatically the hotter lead. | — | — |
| **D3** | ~~Draft-before-review paths~~ **Decided 2026-11-26: drafting is automatic** (section 11). No review gate before drafting; `run` / `batch` / `go` draft at the end of every run. The old `run --draft` / `go` "Both" / batch `draft:` paths are replaced by the one auto step, not kept side by side. | — | — |
| **D4** | `Score Reasons` vs `Audit Notes` (P5) | one field · two fields | **Two**: facts for drafting, reasons for you. |
| **D5** | ~~Verdict for hand-made pages~~ **Obsolete:** no Verdict gate (section 11). Hand-made pages draft like any other page. | — | — |

The edits below assume the recommendations. Change them if you decide otherwise.

---

## 4. Edits by file

### `pipeline/schema.py`
- **CHANGE**: `SCHEMA_VERSION = 10`, `SCORING_VERSION = 4`.
- **ADD** fields, all defaulting to `None` / empty so v9 JSONL loads:
  - collect: `maps_category`, `business_status`, `listing_count`
  - audit: `site_reachable: bool | None`, `copyright_year`, `has_viewport_meta`, `has_booking`, `has_click_to_call`
  - scoring: `fit_score`, `need_score`, `viability_score`, `reach_score`, `priority`, `tier`, `score_reasons: list[str]`, `score_confidence`, `disqualify_reason`
- **CHANGE**: status comment gets `disqualified`. Keep `reachability_score` / `opportunity_score` / `total_score` for one version (see DELETE).

### `pipeline/collectors/maps.py`
- **CHANGE** `parse_results_csv` (lines 116-145): keep `row["category"]` → `maps_category`, and `row.get("status")` → `business_status` (after G5 verification).
- **ADD** a fixture row with a mismatched category and one with a closed status.

### `pipeline/dedupe.py`
- **CHANGE** `dedupe_leads` / `_merge_duplicate`: count how many raw rows merged into each kept lead → `listing_count`. The key is still computed only here. **Decided 2026-11-27:** distinct listings per source (by address, else listing page), highest across sources — raw rows over-count OSM+Maps pairs and grid-cell repeats.

### `pipeline/runner.py` — collect
- **CHANGE** lines 120-125: skip keys found in `seen_domains.json` **or** `rejected_domains.json`. `--include-seen` overrides both. Add `already_rejected` to the manifest counts.

### `pipeline/enrichers/audit.py`
- **FIX G1**: split `enrich()`:
  1. `_probe()`. On failure, return `site_reachable=False` with no exception (the site is dead: that's data, not an error).
  2. Set `https`, `cms` and the signals from the probe HTML **before** Lighthouse runs.
  3. Run Lighthouse in its own `try`. On failure, add the error to `errors` and return the lead with the probe fields kept, `site_reachable=True`.
  - Result: a Lighthouse failure costs 1 run, not 2, and loses nothing the probe found.
- **ADD** `extract_signals(html) -> dict` (pure, fixture-tested): `copyright_year` (the highest year near ©/copyright, ≤ current year), `has_viewport_meta`, `has_booking` (known widgets plus forms with a date input), `has_click_to_call` (`tel:` href). Reuse `detect_cms` for the builder.
- **CHANGE**: `build_audit_notes` stays the *enrich-time* note. The scorer still overwrites it (P5).
- ~~**FIX G3**~~ **Decided 2026-11-27: dropped.** With the split, a dead site and a Lighthouse failure are data (`site_reachable=False` + `probe_error`, or a line in `errors`), not an exception, so the audit never marks a lead `enrich_failed` and re-runs don't repeat Lighthouse. **Also decided:** the probe failure kind is stored as `probe_error` (`dns` / `refused` / `timeout` / `http_error` / `ssl` / `other`), so step 5 treats only `dns` / `refused` as a dead domain; `copyright_year` also looks next to "all rights reserved" and "sva prava zadržana/zadrzana"; ads and messaging links are `ads_tags` and `has_messaging_link`; the core lives in `pipeline/audit.py`; `--only` requires `--rescan`.

### `pipeline/channels.py` (new) + `pipeline/drafting/channel.py`
- **ADD**: move `is_placeholder_phone`, `is_serbian_mobile`, `is_placeholder_email` and the ranking into `pipeline/channels.py`:
  - `usable_channels(lead) -> list[str]` in order `Instagram, Facebook, Viber/Whatsapp, Email, Call`
  - `best_channel(lead) -> str`
- **CHANGE**: `drafting/channel.py` re-exports `suggest_channel = best_channel`, so `test_channel.py` and the drafter keep working.
- `scorer.py` imports from `pipeline.channels`. That's pure code, so the scorer stays pure.

### `pipeline/scoring/` — `scorer.py` (+ `gates.py`)
- **REPLACE** `_qualifies`, `_reachability_score`, `_reachability_multiplier`, `_opportunity_score` with:
  - **ADD** `gates(lead, cfg) -> str | None`, applied in order:
    1. country
    2. `business_status` closed. **Decided 2026-11-27 (after the first real gosom CSV):** closed = `business_status`
       contains "closed" (case-insensitive; `maps_language` is `en` in every market). Reason `closed: temporarily`
       when the text also contains "temporarily", otherwise `closed: permanently`. The real CSV (Novi Sad, 20 open dentists) has the
       `status` column, empty for open places; the exact closed text is still unseen, so match on "closed" only.
    3. chain: `listing_count ≥ chain_listing_min` or the domain is in `exclude_domains`
    4. `exclude_categories`
    5. `maps_category` mismatch, only if `gates.category_mismatch: true` (default off until you've seen the numbers)
    6. no usable channel
  - **ADD** `niche_match()`: Maps category vs the run niche's accepted categories → `match` / `unknown` / `mismatch` (section 9).
  - **ADD** `need()`, by case:
    - `no_website` → new_build: base 60, +25 if IG/FB present, +15 if `review_count ≥ 20`
    - `dead_site` (`site_reachable is False`) → new_build-like: base 50, plus the same IG/FB and review add-ons, reason "domain dead/parked"
    - `audited` → redesign points: not mobile 30 · no HTTPS 20 · outdated 20 · perf < 50 15 · no booking and no call 10 · SEO < 70 5
    - `no_audit` (site live, Lighthouse failed) → redesign points without the Lighthouse items (viewport stands in for mobile), `confidence = low`
  - **ADD** `viability()`: `min(60, 20·log10(reviews+1))` plus rating bands (40/30/15/0). No data → 50, `confidence = low`.
  - **ADD** `reach()`: values from config for `channels.best_channel(lead)`.
  - **ADD** priority and tier: `priority = (0.6·need + 0.4·viability) × (0.5 + reach/200)`. Tiers A ≥ 65, B ≥ 45, C ≥ 25, else D. Low confidence caps the tier at B.
- **CHANGE** `lead_type` mapping: `no_website` / `dead_site` → `new_build`, `audited` → `redesign`, `no_audit` → `unclear`. Today's "unreachable" is now two cases (G2).
- **CHANGE** `_build_audit_notes` (P5, G12): keep the who-they-are, web-presence and contact sentences. **Remove** the rating sentence and the priority sentence, and put them in `score_reasons` instead.
- **DELETE**: the `assert cap < no_website_score` (D2).
- **Keep** the module free of I/O. `score()` keeps its signature but takes the v4 `scoring` dict.

### `pipeline/config.py` + `config.yaml`
- **REPLACE** the `scoring` defaults (lines ~83-101) and `_validate` (lines 229-264) together (G9):
  - weights sum to 1.0 · tiers descending · reach values 0-100 · every market preset complete (section 9)
  - remove the `audited_score_cap` rule
- **ADD** `scoring.gates: {chain_listing_min: 3, exclude_domains: [], exclude_categories: [], category_mismatch: false}`.
- **CHANGE** `config.yaml` `scoring` block to v4 (shape as in PIPELINE.md v2: `markets:` presets, no niche anywhere).
- `qualification.target_countries` moves under `scoring.gates.target_countries`, or stays where it is. Pick one and validate it.

### `pipeline/runner.py` — score
- **CHANGE** `cmd_score`:
  - set `status = "disqualified"` when `disqualify_reason` is set
  - write those keys to `data/rejected_domains.json` (new small helper next to `load_seen`)
  - manifest gains `tier_counts`, `disqualify_reasons`, `confidence_low`
  - the top-5 print sorts by `priority` and shows the tier

### `pipeline/sinks/notion_sink.py`
- **CHANGE** `to_notion_properties`:
  - add `Tier` (select), `Priority` (number), `Score Reasons` (text joined with ` · `)
  - stop writing the old score properties once D-version cleanup lands
  - `Status` logic unchanged
- **ADD** `score_properties(lead) -> dict`, the *only* properties `--rescore` may write. Test that it contains no human field.
- **CHANGE** `lead_from_page`: also read `Verdict` (only `Skip` matters) and `Score Reasons` (not passed to the prompt).
- **CHANGE** `query_draftable`: superseded by section 11 (no Verdict = Go, no SM Audit requirement).
- **ADD** (decided 2026-11-26) Notion text property `Niche Label`: written from `lead.niche_label` by `to_notion_properties`,
  read back by `lead_from_page` → `lead.niche_label`, so `draft-notion` catch-up gets the draft phrase. `Industry` still
  holds the niche name. Until this lands (step 1–5), drafting from a Notion page falls back to `Industry`.

### `pipeline/runner.py` — sync
- **CHANGE** `cmd_sync` (lines 543-562): `to_sync` = `scored` leads with `qualified` and tier ≥ `--min-tier` (default C) (G6).
- **ADD** `--rescore` (G7, G8): loop over `synced` leads → find the page via `cache_key`, then the email index → `update_page(page_id, score_properties(lead))`. Report updated / no page / failed. Respect `--limit`.

### `pipeline/runner.py` — drafting paths (D3, superseded by section 11)
- **DELETE**: `run --draft`, `go` mode "Both", batch key `draft`, the `cmd_draft` stage in `cmd_run` / `cmd_batch`, and the batch "paused → resume at draft" branch (lines 699-704, 723-734).
- **DECIDE** whether to remove the `draft` command itself, or keep it hidden for `scripts/backfill_drafts.py`. Recommended: delete both the command and the script once `--rescore` covers updating existing pages.

### `score-report` (new command)
- **ADD**: read every `data/runs/*/scored.jsonl`, take the latest row per `cache_key`, then query all Notion pages and join them by key or email index (G16). Print:
  - Go / Maybe / Skip counts and Go-rate by tier, lead_type, industry and confidence
  - `Skip Reason` counts
  - once `Outcome` exists: reply and meeting rate by tier and channel
  - a warning on any band with fewer than 30 leads

### Notion (manual, one time)
- **ADD** properties: `Tier`, `Priority`, `Score Reasons`, `Verdict` (optional, only `Skip` is used), `Skip Reason`, `Outcome`, Status option `Draft failed`.
- **ADD** a send view: Status Queued, sorted Tier ↑ then Priority ↓.
- **CHANGE** default view: hide Reachability / Opportunity / Total Score / Lighthouse (G13).
- **Migration**: none needed for drafting (no Verdict gate). Existing queued pages keep their drafts.

---

## 5. Tests to rewrite or add

At this commit, `test_scoring.py` (78 test/assert lines), `test_notion_sink.py` (30 tests), `test_score_stage.py`,
`test_sync_stage.py`, `test_batch_and_credits.py` and `test_channel.py` encode v3 behaviour. Expect most of
`test_scoring.py` to be replaced, not patched.

- Scoring golden leads, one per case (each asserts tier and reasons):
  - no_website with IG
  - dead_site
  - audited good site (Need ≈ 0)
  - audited bad site
  - no_audit
  - OSM lead without rating
  - placeholder-phone-only lead (reach 0 → disqualified)
  - chain
  - closed
- `extract_signals` against `tests/fixtures/contact_page.html` plus 2 new fixtures (old site with ©2017 and no viewport; modern site with a booking widget).
- `audit.enrich` when Lighthouse raises: the probe fields are kept and the step isn't retried.
- `score_properties` never contains `Verdict`, `SM Audit`, `Status`, `Communication`, `Draft Message`.
- `query_draftable` filter: see section 11.4.
- `--rescore`: cached page updated; lead without a page reported, not crashed.
- v9 JSONL loads under schema v10 (all new fields default).
- Config: a v3-shaped `scoring` block fails with a clear message ("scoring v4 expected, see SCORING.md").

## 6. Docs to update

`SCORING.md` (full rewrite; fixes G17, records D1/D2 in the changelog as v4) · `PIPELINE.md` (replace with v2) ·
`ARCHITECTURE.md` §5 (schema) and §6.1 (Notion mapping) · `EXPORT.md` (new properties, `--min-tier`, `--rescore`) ·
`OUTREACH.md` (Verdict gate, Score Reasons not in prompt) · `ENRICHMENT.md` (probe/Lighthouse split, signals) ·
`ROADMAP.md` (status block) · `CLAUDE.md` (commands: drop `draft`/`--draft`, add `score-report`, `--rescore`) ·
`docs/pipeline-map.html`.

## 7. Order (each step ships on its own, tests green after each)

> Superseded by the build order in `strategy_gaps.md` §E, which puts markets and niche-on-go first. Kept for reference.

1. **`pipeline/channels.py`**: move the channel logic, re-export from `drafting/channel.py`. Zero behaviour change.
2. **Audit split + signals** (G1, G2, G3) and **schema v10** fields. Better data even under the v3 scorer.
3. **Collect**: `maps_category`, `listing_count`, `business_status` (after checking a real CSV), the rejected-domains skip.
4. **Scorer v4** + config/validation + `cmd_score` manifest. Run it on one existing run next to v3, compare top 20, and adjust tiers.
5. **Notion properties**, then `to_notion_properties`, `query_draftable`, `--min-tier`, `--rescore`.
6. **Auto-drafting** (section 11).
7. **`score-report`** after your first review session: Go-rate per market and per niche.
8. **Cleanup** one version later: remove `reachability_score` / `opportunity_score` / `total_score` (schema 13; v10 = step 1 market/niche, v11 = step 3 audit signals, v12 = step 4 collect) and their Notion columns.

Follow-ups after v2 (not in this change): Instagram bio and post enrichment for IG-only businesses (G15, ROADMAP to-do #2),
Phase B follow-ups with the `Outcome` field, reply drafts.

## 8. Watch out for

- **Old runs have no signals.** Run `enrich` again on them (audit step only) before `score` v4, or every audited
  lead loses the "outdated" and "booking/call" points. Add `enrich --only audit --rescan` for that.
- **`--rescore` speed:** at 0.35 s per request, 1,000 pages take about 6 min. Run it with `--limit 10` first.
- **Category mismatch gate:** keep it off until you've seen how noisy Google's categories are for Serbian businesses.
- **Thresholds:** check `tier_counts` in the manifest on the first two v4 runs before you trust A/B/C.


---

## 9. Markets and niche-on-go (decisions K1–K3)

### 9.1 Remove default niches (K1)

| File | Edit |
| --- | --- |
| `config.yaml` | **DELETE** `collect.categories`, `collect.country`, `qualification:`. **ADD** `markets:` presets (serbia, uae; shape in PIPELINE.md). |
| `pipeline/config.py` | **DELETE** `outreach.industry_labels` from `DEFAULTS` (8 niches hard-coded) and `collect.country: "Serbia"` / `qualification.target_countries`. **CHANGE** `_validate`: drop the "collect.country must be non-empty" check (`config.py:164`) and the industry_labels check (line ~213). **ADD** market preset validation (required keys, `language ∈ {sr, en}`, `channels` known, `first_touch_excluded ⊆ channels`). |
| `pipeline/runner.py` collect | **DELETE** the `industry_labels` warning (`runner.py:78-86`). `categories` no longer falls back to config (`runner.py:68-71`). It comes from the niche's `search_terms`. |
| `pipeline/schema.py` | **ADD** `market: str`, `language: str`, `niche: str` (name), `niche_label: str` (draft phrase), `niche_match: str | None`. **Keep `industry`** and set it to the niche name, so the Notion `Industry` select keeps working (decided 2026-11-26). `niche` is the same value; no separate Notion column. **Decided 2026-11-27:** `niche_match` moves to step 5 (niche-match check in scorer v4); step 1 adds only `market`, `language`, `niche`, `niche_label`. |
| **ADD** `pipeline/niche.py` | `@dataclass Niche(name, search_terms, phrase, accepted_categories=[], question="", landing_url="", need_overrides={})` + `from_dict` / `to_dict` + validation (name, search_terms and phrase required). **Decided 2026-11-26:** also `osm_tags: list[str]` (optional; raw `key=value` tags or `osm.resolve_category` keys). `search_terms` go to Maps only; OSM uses `osm_tags`. `go` asks for `osm_tags` only when the source includes osm and validates them up front; a run with source osm and no `osm_tags` fails with a clear message. **Decided 2026-11-27:** `need_overrides` moves to step 5 (scorer v4 is its only reader); step 1 ships `Niche` without it. |
| **ADD** `pipeline/market.py` | `@dataclass Market(id, country, dial_code, mobile_prefixes, language, channels, first_touch_excluded, review_strong, currency, maps_language, exclude_name_patterns)` + `load_market(config, id)` + `Market.from_prompts()` for "new market". |
| `cmd_go` (`runner.py:799-855`) | **CHANGE** Lead Gen prompts: market (numbered list of presets + "new market") → niche (name, search terms, draft phrase, accepted categories, question, landing URL; required ones re-asked if empty) → source → location. Optional "copy niche from run X" (lists earlier manifests, nothing preselected). Prints a summary, then confirms. A new market is printed as YAML to paste into config. It is never written to config automatically. |
| `cmd_collect` / `cmd_run` | **ADD** `--market <id>` and `--niche-file <yaml>` (scripted runs). Without a niche the command fails: `"no niche: run go, or pass --niche-file"`. Stamp `market`, `language`, `niche`, `niche_label`, `country` on every lead. Write `market` and `niche` blocks into `manifest.json`. |
| `cmd_batch` / `_BATCH_KEYS` (`runner.py:624`) | **CHANGE**: each entry requires `market:` (preset id or full block) and a full `niche:` block. **DELETE** `categories`, `country`, `draft`, `no_draft` keys. Update `batches/example.yaml` (it uses `detailing`). **Decided 2026-11-27:** step 1 deletes only `categories` and `country`; `draft` / `no_draft` stay until step 9 (auto-drafting, §11) replaces the draft-before-sync paths, so batch drafting keeps working until then. |
| `config/outreach.md` | **SPLIT** into `config/outreach.sr.md` (current content, with niche subsections kept as optional matches) and `config/outreach.en.md` (**you write it**: channel sections + `### generic`). The "Questions by business type" section becomes a generic question list; per-niche questions come from the niche definition. |

### 9.2 Markets and English (K2)

| File | Edit |
| --- | --- |
| `pipeline/channels.py` (new, from §4) | Takes `Market`. `is_mobile(phone, market)` replaces `is_serbian_mobile` (`+3816` / `+9715` from `mobile_prefixes`). Numbers starting with a local `0` are normalized using `dial_code` first. `usable_channels(lead, market)` follows `market.channels`. `best_channel` skips `first_touch_excluded`. Labels from the market (SR `Viber/Whatsapp`, UAE `WhatsApp`, plus `LinkedIn`). |
| `pipeline/enrichers/contact.py` | Social extraction must keep `linkedin` (used by the UAE order). Phone extraction is already format-agnostic (`_plausible_phone`: `+`/`0`, 8–15 digits), so nothing to change. |
| `pipeline/collectors/maps.py` | `maps.language` from `market.maps_language`. **Decided 2026-11-27:** `maps.language` removed from config; `maps_language: en` for Serbia too (one language of categories/status strings). Raw gosom CSV kept as `data/runs/<id>/maps_raw.csv`. **ADD** UAE `maps.areas` (Dubai, Sharjah, Ajman, Abu Dhabi, Al Ain; boxes in 10.4, VERIFY on a map). |
| `pipeline/collectors/osm.py` | Prefer `name:en` when `market.language == "en"`. Use bbox areas for the UAE (area names are ambiguous). **Done in step 4:** any location in `maps.areas` (or `--bbox`) runs as a bbox query, in every market; `--no-grid` falls back to the name lookup. |
| `pipeline/scoring/scorer.py` | Country gate = `lead.country == market.country`. Viability strong band = `market.review_strong`. Reach by rank in `market.channels`, with excluded channels → 0. Name-pattern gate from `market.exclude_name_patterns`. `score()` gets the `Market` (and `Niche`) as plain arguments, so the scorer stays pure. |
| **ADD** `pipeline/drafting/rules_sr.py`, `rules_en.py` | Move the Serbian `_SYSTEM`, `_BANNED`, `_VIBER_ALLOWED`, `_RATING_RE`, greeting rule (`drafter.py:28-85`) into `rules_sr`. Write `rules_en`: system prompt in English; banned "free", "no obligation", "this week", "lighthouse", "seo ", "couldn't find", "don't have a website", "rating", "review", "stars"; greeting per channel (DM "Hi", email/call "Hello"); script check rejects Cyrillic/Arabic. Both languages ban the patient-records platform terms (S23). |
| `pipeline/drafting/drafter.py` | `validate_message(message, channel, rules)`. `build_prompt` takes `lead.language` → rules + examples file. The niche phrase comes from `lead.niche_label` (replaces `outreach["industry_labels"].get(...)`, `drafter.py:210`). Question and landing URL come from the niche when present. The Viber-specific rules apply only where the market's channel label is `Viber/Whatsapp`. |
| `pipeline/sinks/notion_sink.py` | `to_notion_properties`: **ADD** `Market` (select), `Language` (select). **Keep `Industry`** (select) and write the niche name into it; no `Niche` property. `lead_from_page`: read `Market`, `Language`, and `Industry` → `lead.industry` and `lead.niche`. `draft-notion` skips pages missing `Market` or `Industry` and reports them ("set Market and Industry"). |
| `cmd_draft_notion` | Load the market preset per page (`Market` select → preset id). If a page names a market that isn't in config, skip it with a clear message. |
| Notion (manual) | Properties `Market`, `Language`. `Industry` stays as it is and holds the niche name; new niche names are added as select options automatically on sync. **Migration:** set `Market = serbia`, `Language = sr` on all existing pages. Review view per market. |
| `outreach.price_from` | → per market (`markets.<id>.price_from`, EUR / AED) when prices are turned on. |

### 9.3 Agency lane removed (K3)

Nothing to build. Removed from the plan: agency scoring profile, `Lane` field, LinkedIn intake, kill-rule report.

### 9.4 Tests to add for section 9

- `go` with scripted input: new niche + preset market → manifest contains both blocks, and leads carry `market` / `niche` / `niche_label`.
- `collect` without a niche fails with the clear message, and no config fallback exists.
- Batch entry missing `niche` or `market` is rejected by `_load_batch`.
- `channels`: `+971 50…` is mobile for UAE, not for Serbia. `050…` with UAE normalizes to `+97150…`. UAE `best_channel` never returns WhatsApp for first touch.
- `rules_en.validate_message`: rejects "free", "!" and Arabic/Cyrillic; requires "Hi" on DM and a question.
- Scorer: same lead under Serbia vs UAE gets different reach and viability bands.
- `lead_from_page` round-trip for `Market` / `Industry` / `Language` (Industry → `lead.niche`), and `draft-notion` skips a page without `Market`.
- `config.py`: a market preset missing a key fails validation, and no `industry_labels` key is required anymore.

### 9.5 Docs to update for section 9

`CLAUDE.md` (commands: `go` flow, `--market`, `--niche-file`; "no default niches" in Don't-do) · `docs/COLLECTION.md` (markets, UAE areas) ·
`docs/OUTREACH.md` (rule sets per language, examples per language, Market/Niche needed on pages) · `docs/SCORING.md` (niche match, per-market bands) ·
`docs/EXPORT.md` (new properties) · `README` (batch format) · `ROADMAP.md`.


## 10. Example messages, niche-neutral drafting, UAE areas (2026-11-26)

### 10.1 Examples files
Two files replace `config/outreach.md` (both drafted, review before use):

- `config/outreach.sr.md`: all existing approved messages kept as they are. **NEW:** generic Instagram line, clinic
  and accommodation subsections (Instagram, Facebook, Viber/Whatsapp, Viber redesign), an Email section (clinic +
  generic), a clinic call line, an "Instagram with audit offer" section, and a niche-neutral "Questions" list.
- `config/outreach.en.md`: new. Instagram, Instagram with a friendly warning, Instagram with audit offer, Facebook,
  LinkedIn (connection note + after accept), Email, WhatsApp (after a reply only), Call, Questions.

Both use `### generic` as the fallback and optional `### a | b` niche aliases matched against the run's niche name.

| Where | Edit |
|---|---|
| `drafter.py` `_load_examples` | **CHANGE** path to `config/outreach.{language}.md`. Section match order: run niche name → aliases → `### generic`. Missing file → warning, draft without examples. |
| `drafter.py` `build_prompt` question rule | **REPLACE** the hard-coded detailing / school / wash wording with: the niche's `question` from `go` if set, otherwise "pick one from the Questions section and adapt it". Same text for sr and en. |
| `drafter.py:210` | `niche = lead.niche_label` (the draft phrase from `go`), falling back to `lead.industry`. Drop the `industry_labels` lookup. |
| `rules_en.py` | Greetings "Hi" (DM) / "Hello" (Email, Call). Banned: "free", "AI", "automation", "SEO", "lighthouse", "review"/"rating"/"stars", "you don't have a website", "Dear Sir", "hope this finds you". Opt-out line and signature as in the en Email section. |
| `outreach.first_offer` (config) | **ADD** `question` (default) / `audit`. `audit` picks the "with audit offer" sections. Keep it on `question` until website P0 is live (sample audit page, About, `/analiza` or `/review`). |
| LinkedIn | **ADD** channel `LinkedIn` to channel.py and the drafter (UAE market channel order). Two outputs: connection note (≤300 chars, no question required) and after-accept message. |

**Word conflict to keep in mind:** the website copy says "besplatno ga pregledamo" / "free review", but the drafter bans
"besplatno" / "free" in first messages (Viber excepted). The audit-offer lines describe the work ("one-page note with
three things to improve") instead of pricing it. If you want the word, allow it only in the audit-offer line.

### 10.2 Notion `Industry` is kept
`Industry` (select) stays and holds the niche name created in `go`. There is no `Niche` property. New Notion
properties are only `Market` and `Language`. The migration no longer copies Industry into anything.

### 10.3 Tests
- `_load_examples` picks `outreach.en.md` for `language: en`, matches an alias section, falls back to `### generic`.
- `build_prompt` contains no "detailing", "school", "wash" text; contains the niche question when given.
- `rules_en` rejects a draft with "!", "free", a link, or two questions.

### 10.4 UAE `maps.areas` (approximate, VERIFY once on a map)
Format is gosom's `minLat,minLon,maxLat,maxLon`. Built from known city centres and coastline; cell counts use the
repo's `grid_cell_count` at `grid_cell_km: 6`, time at ~28 s measured per cell (upper bound = 60 s timeout).

```yaml
maps:
  areas:
    Dubai:     "24.95,55.05,25.36,55.45"   # Jebel Ali to Al Mamzar, inland to Mirdif/Academic City. 40x46 km, 56 cells, ~26-56 min
    Sharjah:   "25.26,55.36,25.41,55.52"   # city only, not the emirate. 16x17 km, 9 cells, ~4-9 min
    Ajman:     "25.36,55.42,25.44,55.56"   # 14x9 km, 6 cells, ~3-6 min
    Abu Dhabi: "24.30,54.30,24.56,54.66"   # island, Khalifa City, Saadiyat, Yas. 36x29 km, 35 cells, ~16-35 min
    Al Ain:    "24.13,55.64,24.30,55.82"   # 18x19 km, 16 cells, ~7-16 min
```

Notes: Dubai and Sharjah overlap at Al Nahda/Al Qusais (dedupe handles it). Do not use the Sharjah emirate box
(24.69,55.35,25.63,56.38): mostly desert and the east coast, ~250 cells. For a first test, run Dubai with one
category ("dental clinic") and check the result count before running the rest. For comparison, Beograd is 25 cells.


## 11. Automatic drafting, no manual gates (decided 2026-11-26)

Goal: a run goes from collect to a ready draft in Notion with **no approval step, no confirmation prompt and no
required manual note**. You only open Notion to send. Sending stays a human action.

### 11.1 Flow
```
run / batch / go (Lead Gen):  collect → enrich → score → sync → draft (auto)
                                                        pages synced in this run, tier ≥ outreach.auto_draft_min_tier
```
- Drafting runs after sync and writes to the existing page with `update_page`, so the old "draft before sync because
  fields are only written at page creation" constraint is gone.
- `go` Lead Gen still asks for market, niche and location (that defines the run), then runs everything without
  another prompt. `go --yes` skips the final summary confirmation; `run` / `batch` never prompt.
- Old draft-before-review paths (`run --draft`, `go` "Both", batch `draft:`, the "resume at draft" branch) are deleted
  and replaced by this one step.

### 11.2 Eligibility (replaces the Verdict = Go + SM Audit gate)
A page is drafted when all hold: `Qualified` ✓, `Tier` ≥ `outreach.auto_draft_min_tier` (default `B`), Status
`Not started`, `Draft Message` empty, `Verdict` ≠ `Skip`, `Market` and `Industry` set.
- `SM Audit` is **optional**. If you wrote one, it is still the first praise source.
- `Verdict` is optional: set `Skip` on a page and nothing more happens to it (no draft, no follow-up).

### 11.3 Praise without your note
Without an SM Audit note, the drafter needs facts it can trust.
- **ADD** `profile_facts: list[str]` on `Lead` (next schema bump; v10/v11 are taken by steps 1 and 3), built in enrich from data already fetched, no new
  requests: site title + meta description, services named on the site (`site_text`), Maps category and description,
  OSM tags (e.g. `healthcare:speciality`), photo count when Maps gives it.
- **CHANGE** drafter prompt: praise source order = SM Audit → `profile_facts` → the plain line ("sviđa mi se kako
  radite" / "I like the way you present your services") with `verify_note` "no concrete detail found". Never invent.
- `draft_notes` (verify_note) goes to Notion `Draft Notes` as now, so you see what to double-check while sending.
- Instagram bio scraping stays out (login wall, ToS). If you want IG bios later, that's the DM-automation project.

### 11.4 Never stop, never ask
| Situation | Today | Auto |
|---|---|---|
| `draft-notion` without `--yes` | dry run | **writes** when `outreach.auto_draft: true`; `--dry-run` to preview |
| interactive confirms in `run` / `batch` | some | none |
| validation fails after one retry | skipped | Status `Draft failed`, reason in `Draft Notes`, continue with the next page |
| Claude CLI error on one page | stops | log to `Lead.errors` / `Draft Notes`, continue |
| out of AI credits (exit 3) | batch pauses, resume state | stop drafting for this run, report "N pages undrafted"; the next run (or `draft-notion`) picks them up automatically because eligibility is "Draft Message empty". No resume state file. |
| page missing Market / Industry | — | skipped and counted in the report, no prompt |

`draft-notion` stays as the catch-up command (hand-made pages, pages left undrafted, `--redraft`).

### 11.5 Config
```yaml
outreach:
  auto_draft: true
  auto_draft_min_tier: B     # A only = fewer, safer drafts; C = more volume
  first_offer: question      # audit later (section 10)
```

### 11.6 Feedback without a review step
L2 moves from "Verdict before drafting" to **what you do at send time**:
- Sent as is → good. Edited before sending → keep the edit (later: diff feeds the examples file). Not worth sending
  → set `Verdict = Skip` + `Skip Reason`.
- `score-report`: send-rate and skip-rate by tier / niche / market instead of Go-rate. Same "Go-rate should fall
  with tier" logic, read as "skip-rate should rise with tier".

### 11.7 Files
| File | Edit |
|---|---|
| `runner.py` `cmd_run`, `cmd_batch`, `cmd_go` | **ADD** auto draft step after sync (scoped to this run's synced pages); remove draft-before-sync paths, `input()` confirmations in run/batch, resume-at-draft state; `go --yes`. |
| `runner.py` auto draft step | **Decided 2026-11-26:** drafts from the run's JSONL lead (it carries `niche_label`, `profile_facts`), joined to the Notion page by `page_id` from the sync cache — not rebuilt from the page. Writes go to that page with `update_page`. |
| `runner.py` `cmd_draft_notion` | write by default when `auto_draft`; `--dry-run`; per-page failures never raise; summary: drafted / failed / skipped (reason) / undrafted (credits). |
| `notion_sink.py` `query_draftable` | filter from 11.2; optional `run_id` / page-id list to scope to one run. |
| `drafter.py` | praise order from 11.3; `profile_facts` in the prompt; return a failure result instead of raising. |
| `enrichers/contact.py` or audit core | build `profile_facts`. |
| `schema.py` | `profile_facts`. |
| `config.py` | `outreach.auto_draft`, `auto_draft_min_tier` (validate A/B/C); drop `audited_score_cap` validation (D2). |
| `scorer.py` | drop the cap `assert` (D2). |

### 11.8 Tests
- `run` with a fake drafter: drafts only this run's synced pages at tier ≥ B, never calls `input()`.
- Eligibility: empty Verdict and no SM Audit → drafted; `Skip` → not; tier C with min B → not.
- Validator failure → Status `Draft failed`, next page still drafted. Credit-out → remaining pages untouched, exit code
  reports it, a second call drafts them.
- Drafter prompt without SM Audit contains `profile_facts`; with no facts, the plain praise line rule.
- D2: a redesign lead with high Need can outrank a weak new_build lead; no assert/validation error.
