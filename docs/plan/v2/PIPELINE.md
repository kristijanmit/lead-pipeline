# Lead pipeline — the complete sketch (v2: layered scoring)

One page, end to end: how a business on a map, in any market (Serbia, the UAE or a new one), becomes a reviewed first message in Notion,
in Serbian or English, and
what comes after (follow-ups, replies, retuning). Each stage links to its deep-dive doc. Status markers:
**[built]**, **[changed]** (built, but this sketch changes it), **[new]**, **[planned]**.

What changed vs v1, in one line each:
- Scoring now works in **four levels**: L0 gates, L1 automatic sub-scores, L2 what you do at send time, L3 outcomes. Only L1 is a formula.
- L1 splits the single `total_score` into **Need / Viability / Reach** plus a niche-match check, then a **Priority** score and an **A–D Tier**. Every score comes with the reasons behind it.
- **No default niches.** Every run's niche is created in `go`. **Markets** (country, phone rules, language, channel order) are chosen or created in `go` too, and English/UAE are handled alongside Serbian.
- Reach now uses the same channel order as drafting (Instagram first), not email 50 / phone 40 / social 10.
- Need scores the problems an owner can actually see (mobile, HTTPS, an outdated site), and Lighthouse matters less.
- **Drafting is automatic**: every run drafts its synced pages at tier ≥ B with no approval, prompt or required note. You only open Notion to send (or `Skip`).
- "No website always outranks a bad audited site" is dropped: new_build and redesign compete on Need.
- `sync --rescore` lets a retuned formula update leads that are already in Notion.
- Feedback comes first from what you send or skip, and later from replies.

```
 SOURCES              RUN FOLDER: data/runs/<run_id>/             NOTION (outreach tracker)            YOU
 ───────              ──────────────────────────────             ─────────────────────────            ───
 OSM (Overpass) ─┐
 Google Maps    ─┼─► 1 COLLECT ─► collected.jsonl
 Manual CSV     ─┘        │ dedupe (domain) + skip seen/rejected domains
                          ▼
                     2 ENRICH ──► enriched.jsonl
                          │ discovery → contact → audit (probe + signals [new], then Lighthouse)
                          ▼
                     3 SCORE ───► scored.jsonl + scored.csv
                          │ L0 gates ──► disqualified (kept in JSONL, never synced)
                          │ L1 niche match · Need · Viability · Reach ─► Priority ─► Tier A–D + reasons
                          ▼
                     4 SYNC ──── tier ≥ C ──────────────────► page per lead
                          │ --rescore: score fields only        Tier, Priority, sub-scores, Score Reasons
                          ▼                                            │
                     5 DRAFT (auto, same run) ─── tier ≥ B, Verdict ≠ Skip ─► Draft Message, Draft Notes,
                          │ no prompt, failures → Draft failed               Status = Queued ──────────► L2 AT SEND:
                          ▼                                                                             send as is / edit /
                     manifest.json (counts, tier mix, drafted / failed / undrafted)                    Skip + Skip Reason,
                                                                                                        log `Communication`
                                                              6 FOLLOW-UP [planned] ◄──────────────────┘
                                                                 Follow-up due / Dead, sets L3 Outcome
                                                              7 CALIBRATE [new]
                                                                 score-report: L1 vs L2 send/skip (fast)
                                                                               L1 vs L3 outcomes (slow)
                                                                 → retune weights, scoring_version++, sync --rescore
```

Nothing is ever sent by the pipeline. Every outward message is a human action.

---

## 0. Entry points

| Command | What it chains | Use when |
| --- | --- | --- |
| `go` | interactive menu: **Lead Gen** (1→5, drafting included), **Draft Outreach** (catch-up for undrafted / hand-made pages). Lead Gen asks for the **market** (pick or create) and **creates the niche** (name, search terms, draft phrase, optional accepted Google categories / question / landing URL), then the location. "Both" removed (it skipped review) | day-to-day |
| `run --source … --market … --niche-file … --location …` | 1 → 2 → 3 → 4 → 5, no prompts (`--no-draft` to stop after sync) | one scripted run |
| `batch --file batches/<name>.yaml` | `run` per entry, sequential, `--pause-s` between. Each entry carries full `market` and `niche` blocks, with no defaults | several cities / niches |
| individual stages (`collect`, `enrich`, `score`, `sync`, `draft`, `draft-notion`) | one stage | debugging, re-running a step |
| `score-report` **[new]** | reads Notion + JSONL, prints calibration tables (stage 7) | every ~50 sends |

- Every command takes `--limit N`: dry-run on a few leads before spending rate limits or AI credits.
- `run` builds the sink first, so a missing `NOTION_TOKEN` fails before collect/enrich spend time.
- `batch` state is in `data/batches/<name>.json`. One failed entry doesn't stop the rest; running out of
  AI credits (exit code 3) pauses the batch, and a re-run resumes that entry at the draft step.

## 1. Collect **[built]** — docs/COLLECTION.md

`collect --source osm,maps,manual --location "Novi Sad" --categories "dentist,detailing"`

| Source | Module | Notes |
| --- | --- | --- |
| OSM / Overpass | `collectors/osm.py` | primary; free; **never parallel queries** |
| Google Maps | `collectors/maps.py` (gosom) | supplement; grid mode for `maps.areas` / `--bbox`; `--no-grid` = one plain query. **Keep `business_status`** (permanently/temporarily closed) for L0 |
| Manual CSV | `collectors/manual.py` | `leads_manual.csv` |

Then: stamp `location`/`country` → **dedupe** by normalized domain (`pipeline/dedupe.py`, the only place
this key is computed) → **skip seen and rejected domains** via `data/seen_domains.json` (`--include-seen`
keeps them) → `--limit N`.

**[new]** Before dropping duplicates, count how many raw listings share a domain and store it as
`listing_count`. Three or more listings on one domain usually means a chain or franchise, which L0 uses.

## 2. Enrich **[changed]** — docs/ENRICHMENT.md

`enrich --run <run_id>` runs the enrichers in a fixed order on every lead that is not yet enriched. Failures go to `Lead.errors` and never stop the batch.

| Step | Module | Adds |
| --- | --- | --- |
| `discovery` | `enrichers/discovery.py` | guessed domain for no-website leads (adopt only on phone / full-name match) |
| `contact` | `enrichers/contact.py` | `contact_emails`, `contact_phones`, `social_links`, `site_text`; robots.txt before every fetch |
| `audit` | `enrichers/audit.py` | HTTPS, CMS, Lighthouse (perf / a11y / bp / SEO), `mobile_friendly`, `audit_notes` |
| `audit` signals **[new]** | `enrichers/audit.py` | taken from the HTML the audit probe already fetches (no extra request), and kept even if Lighthouse fails: `site_reachable`, `copyright_year`, `has_viewport_meta`, `has_booking`, `has_click_to_call` |

`signals` exists for two reasons. Need should rest on things an owner can see, and leads whose Lighthouse
run failed (`unclear`) still need something to score on.

## 3. Score **[changed]** — docs/SCORING.md

`score --run <run_id>`: `scoring/scorer.py` stays a **pure function** `(Lead, ScoringConfig) → ScoreResult`.
Every number it returns comes with a short reason string. Weights and thresholds live in
`config.yaml › scoring`. Bump `scoring_version` (now **4**; v3 is the current formula) whenever any of them change.

### The levels

| Level | Who / when | Question it answers | Output |
| --- | --- | --- | --- |
| **L0 Gates** | pipeline, stage 3 | *Can we contact them at all?* | `qualified`, `disqualify_reason` |
| **L1 Auto score** | pipeline, stage 3 | *In what order should I review them?* | niche match, `need`, `viability`, `reach` → `priority` → `tier`, `score_reasons`, `score_confidence` |
| **L2 Send decision** | you, in Notion, at send time | *Is this draft worth sending?* | sent / edited / `Verdict = Skip` + `Skip Reason` |
| **L3 Outcome** | follow-up stage, from the `Communication` log | *Did it work?* | `Outcome` |

L1 decides which leads get drafted (tier ≥ B) and the send order. L2 is still the real decision, made when you send. L3 is the ground truth that L1
is tuned against.

### L0 — gates (filters, no score)

A lead is disqualified, with the first reason that matches, when:

| Gate | Source |
| --- | --- |
| `country` ≠ the run's market country | collect (market) |
| `business_status` = closed | Maps |
| chain: `listing_count ≥ scoring.chain_listing_min` (3), or domain/brand in `scoring.exclude_domains` | collect / config |
| category in `scoring.exclude_categories` (government, pharmacy chains, banks, …) | config |
| no usable contact channel (`reach = 0`) | L1 reach |

Disqualified leads stay in `scored.jsonl` and `scored.csv` but are **not synced**. Their keys go into
`data/rejected_domains.json`, a separate file because `seen_domains.json` entries must have a `page_id`, so collect doesn't fetch them again. `--include-seen`
overrides this.

### L1 — four sub-scores (each 0–100)

**Niche match — instead of industry Fit.** There are no default niches: every lead in a run belongs to the niche you
created in `go`, so niches aren't weighted against each other inside the formula.
- Maps category in the niche's accepted categories → `match`.
- No accepted categories given, or no Maps category → `unknown` (no effect).
- Clearly different category → `niche_mismatch`. That's an L0 gate if `gates.niche_mismatch: true`, otherwise a reason line and tier capped at C.

Learning which niches work happens in `score-report` (Go-rate and outcomes by niche across runs), not in the weights.

**Need — how much their web presence would visibly improve.** The value of `lead_type` decides the branch:

| `lead_type` | Need formula (cap 100) | Confidence |
| --- | --- | --- |
| `new_build` (no domain) | 60, +25 if they have a social link (they're already online and just missing a site), +15 if `review_count ≥ 20` | high |
| `redesign` (domain + Lighthouse) | not mobile-friendly +30 · no HTTPS +20 · outdated (`copyright_year ≤ now−4` or legacy builder) +20 · perf < 50 +15 · no booking and no click-to-call +10 · SEO < 70 +5 | high |
| `new_build` (dead domain: probe `dns` / `refused`) | 50 + the same social/review add-ons; reason "domain dead" | high |
| `redesign` (certificate error: probe `ssl`) | the dead-site formula: 50 + the same add-ons; reason "certificate error, browsers show a warning". `lead_type` stays `redesign` because the site exists | high |
| `unclear` (site didn't load: probe `timeout` / `http_error` / `other`) | 50 + the same add-ons; reason "site didn't load" | **low** |
| `unclear` (site live, Lighthouse failed) | the same as `redesign`, with "not mobile-friendly" read from `has_viewport_meta` and the Lighthouse items left out (max 80) | **low** |

(Decided 2026-11-27.) "No booking and no click-to-call" also needs no Viber/WhatsApp link (S9). A niche's optional
`need_overrides` (Need item → points) replaces these points for that niche (S15). A social page given as the website counts as no website.

In v1, Need came from `100 − Lighthouse`, and owners never see those numbers (you can't mention them in a
message either). Here the biggest weights go to problems a dentist notices on their own phone, and a
modern site lands near 0, which is correct. `unclear` no longer drops out. It gets a provisional score and is flagged.

**Viability — whether they're active and can pay.**
With no rating data (OSM, manual), viability is 50 plus a reason line ("no rating data"). It does **not** lower confidence or cap the tier (decided 2026-11-27). Otherwise:
`volume = min(60, 40·log10(review_count + 1) / log10(review_strong + 1))`: the market's `review_strong` count scores 40
(UAE, 100: 10 reviews → 21, 100 → 40, 1000 → 60; Serbia, 50: 10 → 24, 50 → 40, 300 → 58)
`quality` = rating ≥ 4.5 → 40, 4.0–4.4 → 30, 3.5–3.9 → 15, below that → 0
`viability = volume + quality`, +10 if the site runs ads (Meta Pixel / Google Ads / GTM, S8), capped at 100.
v1 had no such signal. A 4.8★ practice with 300 reviews and a bad site beats a dormant one with 2 reviews.

**Reach — the best single channel**, in the **market's** channel order (one shared `pipeline/channels.py`).
Serbia: Instagram 100 · Facebook 90 · mobile (Viber/WhatsApp, `+3816`) 80 · email 60 · landline 40.
UAE: Instagram 100 · Facebook 90 · LinkedIn 80 · email 60 · landline 40. WhatsApp (`+9715`) is **not** a first-touch channel there, so it's shown but scores 0.
Viability's "strong" review band also comes from the market (Serbia 50+, UAE 100+).
Put this ranking in **one shared function** that `channel.py` and the scorer both import, so the two
can't drift apart again. (In v1, social was worth 10 points even though it's the channel you use first.)

### L1 — combining

```
priority = (0.6·need + 0.4·viability) × (0.5 + reach/200)
           reach 100 → ×1.0 · 80 → ×0.9 · 60 → ×0.8 · 40 → ×0.7

tier     = A ≥ 65 · B 45–64 · C 25–44 · D < 25
           low-confidence Need (unclear: Lighthouse failed, or the site didn't load) → at most B,
           plus "needs manual look" in reasons. A missing rating never caps the tier.
```

- `score_reasons`: kept separate from `audit_notes`. `audit_notes` is the fact sheet the draft prompt reads, so ratings and scores stay out of it. `score_reasons` holds the top 3–5 drivers, e.g. `niche match · not mobile-friendly · no HTTPS ·
  4.7★/212 reviews · best channel: Instagram`. They go into Notion and show you why a lead was drafted.
- `score_confidence`: `high` / `low`. Low only when Need had no audit data to rest on (the two `unclear` rows); that is also
  exactly when the tier is capped at B. A missing rating is a reason line, not low confidence.
- The thresholds are a first guess. The manifest records the tier mix for each run. Aim for A ≈ top 15–20%,
  a number you can review in one sitting, and move the thresholds if a run is mostly A or almost no A.
- One weight set for all lead types for now. Split weights by `lead_type` only if `score-report` shows
  that one type behaves differently.

Output: `scored.jsonl` + `scored.csv` (sub-scores, priority, tier, reasons, disqualify_reason).

## 4. Sync to Notion **[changed]** — docs/EXPORT.md, docs/ARCHITECTURE.md §6.1

`sync --run <run_id> --sink notion [--min-tier C]` creates one page per qualified lead at or above
`--min-tier` (default C, so D and disqualified leads stay in JSONL only). It runs at ~3 req/s.

- Dedupe against the local `seen_domains.json` cache (domain + email), never a live query per lead.
  The cache already stores the Notion `page_id` per key, which `--rescore` uses.
- New properties: `Market`, `Language`, `Tier` (select), `Priority` (number), `Score Reasons` (text, includes the four sub-scores). The database already has ~34 columns, so the sub-scores don't get columns of their own. `Qualified` stays.
- **`sync --rescore --run <id>`** [new]: for leads already in Notion, update **only** the score properties
  above. Human fields (`Verdict`, `SM Audit`, `Status`, `Communication`, drafts) are never touched.
  This is what lets a retune reach the existing pipeline.
- `Industry` (select) is kept and holds the niche name created in `go`; there is no separate Niche column.
- Field mapping stays in `to_notion_properties()`; bookkeeping (`run_id`, `errors`, versions) stays in JSONL.

## 5. Draft first messages, automatically **[changed]** — docs/OUTREACH.md, script_edits.md §11

Runs at the end of every `run` / `batch` / `go` Lead Gen, right after sync, on the pages synced in that run. No
dry run, no confirmation, no required note.

Eligible page: `Qualified` ✓, `Tier` ≥ `outreach.auto_draft_min_tier` (B), Status `Not started`, empty
`Draft Message`, `Verdict` ≠ `Skip`, `Market` and `Industry` set. `SM Audit` is optional.

Per page: channel (shared ranking function, market order) → message (`claude -p`, language rule set sr/en, 2–3
sentences) → validate (one retry) → write `Suggested Channel`, `Draft Message`, `Draft Notes`, `Status = Queued`.
- Praise source: your `SM Audit` if present → `profile_facts` from enrich (site title/description, services,
  Maps category, OSM tags) → the plain praise line, flagged in `Draft Notes`. Never invented.
- Validation failure → Status `Draft failed` + reason; a CLI error on one page → logged; both continue.
- Out of AI credits → drafting stops for this run and reports how many pages are undrafted; the next run or
  `draft-notion` picks them up (eligibility = empty `Draft Message`).
- `draft-notion` remains for catch-up, hand-made pages and `--redraft`. It writes by default (`--dry-run` to preview).
- Ratings and audit numbers are still never written into the message (the validator blocks them).

Then **you** read the draft and `Draft Notes`, edit if needed (or set `Verdict = Skip` + `Skip Reason`), choose `Chosen Channel`, send by hand, and log
`Communication`: `YYYY-MM-DD · SENT · channel · text` / `YYYY-MM-DD · REPLY · text`, plus `Last Contacted`.

## 6. Follow-ups **[planned — Phase B]**

Same as v1: a `followup` stage, Notion-first, dry-run unless `--yes`. SENT with no REPLY after N days →
follow-up draft, `Follow-up date`, Status `Follow-up due`. Still nothing after the follow-up → `Dead`.

**[new]** It also writes the L3 field `Outcome`, derived from the log:
`no_reply` (Dead) · `declined` · `replied` · `meeting` · `won`. The tags `declined` / `meeting` / `won`
come from you, either by hand or through a `REPLY:` keyword convention in `Communication`.
Allowed writes: Status, Suggested Channel, Draft Message, Follow-up date, Outcome. It never touches
`Communication`, `Last Contacted`, `Chosen Channel`, `Notes`, `Verdict`, or `SM Audit`.

## 7. Calibrate **[new]** — replaces "Scoring retune"

`score-report` reads the Notion pages and the JSONL and prints two tables:

| Signal | Available | Compare | Healthy looks like |
| --- | --- | --- | --- |
| **L1 vs L2** | after the first ~50 drafts | send-rate / skip-rate by tier, `lead_type`, niche, market; count of each `Skip Reason` | skip-rate rises A → C |
| **L1 vs L3** | after ~30+ sends per band | reply rate and meeting rate by tier, sub-score band, channel, industry | A clearly beats C |

How to act on it:
- A `Skip Reason` that keeps recurring is a missing **gate**. If "chain" keeps appearing, lower
  `chain_listing_min`. If "closed" keeps appearing, check the Maps status parsing.
- If skip-rate doesn't rise as tier drops, a **sub-score weight** is wrong. Check which sub-score separates Go
  from Skip, and give it more weight.
- If you send most drafts but replies are low, the **message** is the problem, not the score. Feed it into
  `config/outreach.md`.
- Don't retune on fewer than ~30 leads per band. Change one thing at a time, bump `scoring_version`,
  then `sync --rescore`.

Reply drafts and prompt feedback (your Notion edits → approved examples in `config/outreach.md`) stay as v1 ideas.

---

## Config sketch

Markets are facts about a country, so presets can live in config. **Niches never do.** They're created in `go` and stored
in the run manifest, on each lead and in Notion.

```yaml
markets:
  serbia:
    country: Serbia
    dial_code: "+381"
    mobile_prefixes: ["+3816"]
    language: sr              # drafting rule set + examples file config/outreach.sr.md
    channels: [Instagram, Facebook, Viber/Whatsapp, Email, Call]
    first_touch_excluded: []
    review_strong: 50
    currency: EUR
    maps_language: en         # en for every market: one language of Google categories / status strings
    exclude_name_patterns: ["dom zdravlja", "zavod", "opšta bolnica", "KBC", "institut"]
  uae:
    country: United Arab Emirates
    dial_code: "+971"
    mobile_prefixes: ["+9715"]
    language: en              # config/outreach.en.md (first draft written, approve before use)
    channels: [Instagram, Facebook, LinkedIn, Email, Call, WhatsApp]
    first_touch_excluded: [WhatsApp]   # only after a reply (UAE lane doc)
    review_strong: 100
    currency: AED
    maps_language: en
    exclude_name_patterns: []

maps:
  areas:            # UAE, approximate, VERIFY on a map (script_edits.md 10.4)
    Dubai:     "24.95,55.05,25.36,55.45"   # 56 cells
    Sharjah:   "25.26,55.36,25.41,55.52"   # 9 cells
    Ajman:     "25.36,55.42,25.44,55.56"   # 6 cells
    Abu Dhabi: "24.30,54.30,24.56,54.66"   # 35 cells
    Al Ain:    "24.13,55.64,24.30,55.82"   # 16 cells

# niche definition, created by `go` (example of what lands in the manifest, NOT config):
# niche: {name: dental clinic, search_terms: [dentist, dental clinic], phrase: dental clinics,
#         accepted_categories: [Dentist, Dental clinic], question: "", landing_url: ""}

scoring:
  version: 4
  gates: {chain_listing_min: 3, exclude_domains: [], exclude_categories: [], niche_mismatch: false}
  weights: {need: 0.6, viability: 0.4}
  reach_by_rank: [100, 90, 80, 60, 40]   # applied to the market's channel order
  need:
    new_build: {base: 60, has_social: 25, reviews_20plus: 15}
    dead_site_base: 50                    # dead domain, certificate error, site didn't load
    redesign: {not_mobile: 30, no_https: 20, outdated: 20, perf_below_50: 15,
               no_booking_or_call: 10, seo_below_70: 5}
    outdated_years: 4
    legacy_cms: [Joomla]
  viability: {no_data: 50, volume_at_strong: 40, volume_max: 60,
              rating_bands: [[4.5, 40], [4.0, 30], [3.5, 15]], runs_ads: 10}
  tiers: {A: 65, B: 45, C: 25}
```

## Data and state at a glance

| Where | What | Owner |
| --- | --- | --- |
| `data/runs/<run_id>/*.jsonl` | the `Lead` records per stage (source of truth) | pipeline |
| `data/runs/<run_id>/manifest.json` | config snapshot, counts, **tier mix**, disqualify reasons, errors | pipeline |
| `data/seen_domains.json` | key → `page_id`, snapshot (already in Notion) | sync / collect |
| `data/rejected_domains.json` | keys disqualified by L0 gates | score / collect |
| `data/batches/<name>.json` | batch progress | batch |
| Notion database | working list: tier, scores, reasons, verdict, notes, drafts, status, log, outcome | you + draft stages |
| `config.yaml` | sources, grid areas, enrich timeouts, **scoring weights / gates / tiers**, outreach | you |
| `config/outreach.sr.md`, `config/outreach.en.md` | example messages by channel, `### generic` + optional niche subsections | you |
| `.env` | `NOTION_TOKEN`, `NOTION_DATABASE_ID` | you (never committed) |

**Schema v10–v13** (`pipeline/schema.py`; v10 step 1, v11 step 3, v12 step 4, v13 step 5) adds: `maps_category`, `listing_count`, `business_status`, `site_reachable`, `copyright_year`,
`has_viewport_meta`, `has_booking`, `has_click_to_call`, `market`, `language`, `niche` (= `industry`, synced to the Notion `Industry` select), `niche_label`, `niche_match`, `need_score`,
`viability_score`, `reach_score`, `priority`, `tier`, `score_reasons`, `score_confidence`,
`disqualify_reason`. `opportunity_score` / `reachability_score` / `total_score` are kept for one version
for comparison, then removed.

`Lead.status`: `collected` → `enriched` | `enrich_failed` → `scored` | `disqualified` → `synced`.
Notion `Status`: `Not started` → (auto draft) → `Queued` | `Draft failed` → (sent) → `Follow-up due` → `Dead`, or a reply.
Notion `Verdict`: optional, `Skip` stops all further work on a page. Notion `Outcome`: no_reply / declined / replied / meeting / won.

## Principles that hold across every stage

- One `Lead` dataclass flows everywhere; new fields bump `schema_version`.
- Degrade, don't crash: missing data becomes a default plus a reason line (and `score_confidence: low` when Need has no audit data), never an exception or a silent drop.
- **Every score explains itself**: no number without a reason string.
- **The formula drafts, you send**: L1 picks what gets drafted and in what order; nothing is sent without you.
- **One source per rule**: domain key in `dedupe.py`, channel ranking in one shared function,
  Notion mapping in `to_notion_properties()`, weights in `config.yaml`.
- Idempotent re-runs: enrich skips enriched, sync and collect skip cached, drafting skips drafted,
  `--rescore` touches score fields only.
- Zero budget, single operator: no queues, databases or schedulers until ARCHITECTURE.md §14 says so.
