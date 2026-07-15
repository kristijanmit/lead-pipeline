# ONIX lead pipeline — development plan

Six phases, each shippable on its own. Phase 1-3 are what gets a real, scored
dataset flowing; phase 4-6 are what makes it a habit instead of a one-off script.
All still zero-budget except one optional item flagged below.

See ARCHITECTURE.md for the target module structure (`pipeline/collectors/`,
`pipeline/enrichers/`, `pipeline/scoring/`, `pipeline/sinks/`, the `Lead`
schema). Each phase below notes where it naturally lands in that structure —
this isn't a separate refactor to schedule, it's what "doing the phase
properly" means in practice.

## Phase 1 — Data collection

Goal: get a real, live dataset before optimizing anything downstream.
Lands as: `pipeline/schema.py` (the `Lead` shape), `pipeline/dedupe.py`
(domain normalization), `pipeline/collectors/{osm,maps,manual}.py`.

- [ ] Run `collect_osm.py` against ONIX's actual target areas/categories and
      sanity-check the output (does `leads.csv` actually look usable?)
- [x] Change `collect_osm.py` so location and categories are given as input when
      the script is run (CLI args or an interactive prompt), not just buried in
      `config.yaml` — makes it quick to re-target a new city/vertical per run
      (landed directly as `pipeline/runner.py collect --location ... --categories ...`)
- [ ] Add `collect_maps.py` wrapping `gosom/google-maps-scraper` for categories/areas
      where OSM data is thin (use sparingly — gray area on Google's ToS), same
      runtime-input pattern as above
- [ ] Add a `leads_manual.csv` path for LinkedIn/Clutch/referral leads gathered by
      hand, merged with the automated sources before `enrich.py` runs
- [ ] Since there are now three sources feeding one pipeline, this is the
      natural point to split into `pipeline/collectors/` per ARCHITECTURE.md
      §6-7 rather than three more standalone scripts — each satisfies the
      same `Collector.collect(params) -> Iterable[Lead]` interface

## Phase 2 — Enrichment (contact data first, then audit)

Goal: know how to reach a lead before spending effort scoring their website.
Lands as: `pipeline/enrichers/contact.py` and `pipeline/enrichers/audit.py`,
each satisfying the `Enricher.enrich(lead: Lead) -> Lead` interface.

- [x] Notion schema already supports this data — `Primary Email`,
      `Additional Emails`, `Phone`, `Social Links`, `CMS`, `HTTPS`,
      `Mobile Friendly`, and the four Lighthouse breakdown scores all exist
      in ONIX Leads Pipeline now, so this phase just needs to produce the data
- [ ] Expand contact enrichment in `enrich.py` (-> `enrichers/contact.py`): pull
      phone numbers (regex) and social media links (LinkedIn, Instagram,
      Facebook, X) from the homepage/contact page, alongside the existing
      email scrape — and keep every email found, not just the first one
- [ ] Only once contact data is gathered, run the website audit
      (-> `enrichers/audit.py`): install Lighthouse CLI
      (`npm install -g lighthouse`), replace the PageSpeed API call in
      `enrich.py` with a local `lighthouse <url> --output=json` run — same
      engine, no API key, no quota, plus accessibility, SEO, and
      best-practices scores (PageSpeed API only gave you performance)
- [ ] Update `Audit Notes` to summarize contact-data completeness alongside
      all four Lighthouse categories, not just speed

## Phase 3 — Scoring refinement

Goal: the score should reflect real opportunity, not just what was easy to measure.
Lands as: `pipeline/scoring/scorer.py` — a pure function, `score(lead, weights)
-> lead`, no I/O — see ARCHITECTURE.md §13 on why this is the highest-value
thing in the whole project to unit test.

- [ ] Expand `website_audit_score` in `score.py` to weight accessibility/SEO/
      best-practices alongside performance (right now it's performance-only)
- [ ] Add a manual "Intent Score" workflow: quick LinkedIn/news check on your
      top 20 candidates before final ranking, entered by hand
- [ ] After the first real batch, tune `icp_scoring` weights in `config.yaml`
      against which leads actually replied — don't guess the weights twice

## Phase 4 — Notion sync

Goal: scored leads land in the tracker without manual CSV wrangling every time.
Lands as: `pipeline/sinks/csv_sink.py` and `pipeline/sinks/notion_sink.py` —
see ARCHITECTURE.md §6.1 for the exact `Lead` -> Notion property mapping
(multiple emails, social links, and Lighthouse breakdown don't map 1:1).

- [ ] Decide the sync path: paste-to-chat bulk create (works today, zero setup)
      vs. a dedicated Notion integration token used directly from Claude Code
      (removes the manual step, needs a one-time free token setup)
- [ ] Implement `to_notion_properties(lead)` in `notion_sink.py` per
      ARCHITECTURE.md §6.1 — first email to `Primary Email`, the rest joined
      into `Additional Emails`, social links rendered as markdown, Lighthouse
      categories flattened into their four separate score columns
- [ ] Add a dedupe check — a local `seen_domains.json` cache refreshed
      periodically from Notion, not a live query per lead (per ARCHITECTURE.md
      §9, this is what keeps sync fast once the database has a few hundred rows)
- [ ] Confirm the Status field moves cleanly: Not started → In progress → Done

## Phase 5 — Outreach handoff

Goal: turn a scored lead into a sent, personalized message with minimal typing.

- [ ] Draft an outreach template that references the specific audit finding per
      lead (e.g. "noticed your site isn't mobile-optimized and scores X on
      Lighthouse performance") instead of a generic opener
- [ ] Send the first batch manually (no budget for a sequencer yet — Instantly/
      Smartlead free tiers are an option once volume justifies it)
- [ ] Log sent/replied status back into the Notion Status field by hand for now

## Phase 6 — Feedback loop

Goal: the scoring model gets better every cycle instead of staying static.

- [ ] After 50-100 leads have gone through outreach, pull which ones replied
      and cross-reference their scores
- [ ] Adjust `scoring_weights` and `icp_scoring` in `config.yaml` based on that data
- [ ] Re-run collection → enrich → score on a regular cadence (weekly/monthly)

## Rough timeline

- Week 1: Phase 1 + 2 (get real data flowing, then sharpen the audit)
- Week 2: Phase 3 (better scoring, once there's real data to tune against)
- Week 3: Phase 4 + 5 (sync + first real outreach batch)
- Ongoing: Phase 6

## Risks & constraints

- Overpass/OSM shares a public rate limit — don't hammer it with parallel queries
- Google Maps scraping sits in a ToS gray area — use as a supplement, not primary
- Realistic email hit rate without a paid data source: 40-60%, not 90%+

## Success metrics to actually track

- Leads collected per week
- % of leads with a verified (non-invalid) email
- Reply rate on sent outreach
- Time from "collected" to "Notion-ready, scored lead"
