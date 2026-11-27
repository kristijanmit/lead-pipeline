# AGENCY lead scoring — design notes (scorer v5)

## 1. Overview

`pipeline/scoring/scorer.py` implements
`score(lead, scoring, market, niche, year) -> Lead`, a pure function with no
I/O (ARCHITECTURE.md §6/§13): the `scoring:` block from `config.yaml`, the
run's `Market` and `Niche` (both from the run's `manifest.json`) and the
current year all arrive as arguments. It returns a new `Lead` and never
mutates the input.

This document is the "why" behind the model and a reference to the code. The
target design is `docs/plan/v2/PIPELINE.md` §3; where the two differ, this
file describes what the code does.

Scoring works in four levels. Only L0 and L1 are in the scorer:

| Level | Who / when | Question | Output |
| --- | --- | --- | --- |
| **L0 Gates** | `score`, stage 3 | Can we contact them at all? | `qualified`, `disqualify_reason`, `status: disqualified` |
| **L1 Auto score** | `score`, stage 3 | In what order should I review them? | `niche_match`, `need_score`, `viability_score`, `reach_score` → `priority` → `tier`, `score_reasons`, `score_confidence` |
| L2 Send decision | you, in Notion | Is this draft worth sending? | sent / edited / `Verdict = Skip` + `Skip Reason` |
| L3 Outcome | follow-up stage (planned) | Did it work? | `Outcome` |

L2 is the real decision; L3 is the ground truth L1 is tuned against (§11).

| Section | Concept | Function in `scorer.py` |
| --- | --- | --- |
| §2 | L0 gates | `gate()` |
| §3 | Niche-match check | `niche_match()` |
| §4 | Which Need branch / `lead_type` | `audit_case()`, `_CASES` |
| §5 | Need | `need()`, `_new_build_need()`, `_redesign_need()` |
| §6 | Viability | `viability()` |
| §7 | Reach | `reach()` + `pipeline/channels.best_channel()` |
| §8 | Priority, tiers, confidence | `score()`, `tier_for()`, `_cap()` |
| §9 | `score_reasons` and `audit_notes` | `score()`, `_build_audit_notes()` |

All weights and thresholds live in `config.yaml › scoring` (`version: 5`).
Per-market values live in `config.yaml › markets` (§10).

## 2. L0 gates — filters, not scores

A lead is disqualified by the **first** gate that matches, in this order:

| Gate | Reason string | Source |
| --- | --- | --- |
| `lead.country` ≠ the market's country | `country: X is not Serbia` | collect (market) |
| `business_status` contains "closed" | `closed: temporarily` / `closed: permanently` | Maps |
| `listing_count ≥ gates.chain_listing_min` (3) | `chain: 4 listings` | collect |
| domain in `gates.exclude_domains` | `chain: excluded domain x.rs` | config |
| `maps_category` in `gates.exclude_categories` | `excluded category: Pharmacy` | config |
| company name matches one of the market's `exclude_name_patterns` (whole words, case-insensitive) | `excluded name: dom zdravlja` | market |
| `gates.niche_mismatch: true` and the niche match is `mismatch` (§3) | `niche mismatch: Pharmacy store` | niche |
| no first-touch channel (`best_channel` is empty, §7) | `no usable contact channel` | channels |

A disqualified lead gets `qualified=False`, `status: disqualified`, its
`disqualify_reason`, `niche_match`, `lead_type` and `audit_notes`, and
`score_reasons = ["disqualified: <reason>"]`. Need, Viability, Reach,
priority, tier and confidence stay `None`. It stays in `scored.jsonl` and
`scored.csv` but `sync` never pushes it, and `score` writes its dedup key to
`data/rejected_domains.json` (with reason and run id) so `collect` skips it
next time (`--include-seen` overrides). If a later re-score lets the lead
through, its key comes off that file again.

The manifest counts disqualifications by reason kind (`country`, `closed`,
`chain`, …). A kind that keeps showing up as a `Skip Reason` at send time
instead is a missing gate (§11).

## 3. Niche-match check — instead of industry Fit

**There are no default niches** (strategy_gaps.md K1). Every run's niche is
created in `go` or spelled out in full (`--niche-file`, batch `niche:`), and
every lead in the run belongs to it. Ranking niches against each other inside
one run makes no sense, so **industry Fit is gone from the formula**. In its
place is a check that the lead really is in the niche:

| Situation | `niche_match` | Effect |
| --- | --- | --- |
| `maps_category` is in the niche's `accepted_categories` (case-insensitive) | `match` | reason line "niche match" |
| niche has no `accepted_categories`, lead has no `maps_category` (OSM, manual), or the run has no niche (old run) | `unknown` | none |
| `maps_category` is not in the list | `mismatch` | L0 gate if `gates.niche_mismatch: true`; otherwise reason line "niche mismatch: X" and tier capped at C |

Which niches work is learned across runs in `score-report` (send rate,
replies and the A/B rule by niche, §11), not in the weights.

## 4. Which Need branch applies — `audit_case()` and `lead_type`

One helper decides the branch, so `lead_type`, Need and `audit_notes` can
never disagree:

| `audit_case` | When | `lead_type` | Need confidence |
| --- | --- | --- | --- |
| `no_website` | no domain, or the "website" is a social page / non-site link (`dedupe.is_social_domain`: Instagram, Facebook, TikTok, YouTube, Linktree, wa.me, Maps links…) | `new_build` | high |
| `dead_site` | probe failed with `dns` / `refused` — nothing is there | `new_build` | high |
| `cert_error` | probe failed with `ssl` — the site exists, browsers show a warning | `redesign` | high |
| `probe_failed` | probe failed with `timeout` / `http_error` / other — may be temporary | `unclear` | **low** |
| `audited` | site reachable and Lighthouse returned data | `redesign` | high |
| `no_audit` | site reachable but Lighthouse failed, or an old run with no probe signals | `unclear` | **low** |

`unclear` no longer drops out of scoring (it did in v3): it gets a
provisional score from what the probe saw, is flagged "needs manual look", and
its tier is capped at B (§8). A lead whose enrichment never reached it
(status still `collected`) is passed through unscored by `cmd_score` rather
than scored as "site didn't load".

## 5. Need — how much their web presence would visibly improve (0–100)

Need scores what an owner can see on their own phone, not Lighthouse numbers
(which they never see and a message can't mention). A modern site lands near
0, which is correct. Capped at 100.

**No website / dead site** (`scoring.need.new_build`, `dead_site_base`):

```
base  = new_build.base (60)     no_website
      = dead_site_base (50)     dead_site, cert_error, probe_failed
need  = base
      + new_build.has_social (25)     if they have Instagram or Facebook
      + new_build.reviews_20plus (15) if review_count ≥ 20
```

The social add-on means "already online, just missing a site". First reason
line: "no website" / "domain dead (dns)" / "certificate error, browsers show a
warning" / "site didn't load (timeout)".

**Has a site** (`scoring.need.redesign`), one item per problem found:

| Item | Points | Hit when | Reason line |
| --- | --- | --- | --- |
| `not_mobile` | 30 | Lighthouse `mobile_friendly` is False; without Lighthouse, `has_viewport_meta` is False | not mobile-friendly |
| `no_https` | 20 | `https` is False | no HTTPS |
| `outdated` | 20 | `copyright_year ≤ year − outdated_years` (4), else `cms` in `legacy_cms` ([Joomla]) | outdated (© 2019) / outdated builder (Joomla) |
| `no_booking_or_call` | 10 | no booking widget, no `tel:` link and no Viber/WhatsApp link | no booking, tap-to-call or messaging link |
| `perf_below_50` | 15 | Lighthouse performance < 50 | slow site |
| `seo_below_70` | 5 | Lighthouse SEO < 70 | weak SEO basics |

`audited` uses all six. `no_audit` (Lighthouse failed) leaves out the two
Lighthouse items, so it maxes at 80; for an old run without probe signals it
adds "no audit signals (old run: enrich --only audit --rescan)". No hit at
all gives the reason "site looks fine".

**Niche overrides** (S15): a niche's optional `need_overrides` replaces the
points of any `redesign` item for that niche, e.g. accommodation
`{no_booking_or_call: 0}` because a booking widget is the whole job there. An
item set to 0 is skipped and gets no reason line. Keys are validated against
`scorer.NEED_ITEMS`.

## 6. Viability — active and able to pay (0–100)

With no rating and no review count (OSM, manual): `viability = no_data` (50)
and the reason "no rating data (viability 50)". This is **not** low
confidence and never caps the tier.

A Maps listing with **no reviews yet** (review count 0 and rating 0 or empty;
Google reports a 0 rating for a listing nobody has reviewed): volume 0 +
`no_reviews_quality` (20), reason "no reviews yet". That's neither a 0★
rating nor missing data: it's a new or quiet listing, so it scores below a
lead we know nothing about (50) but above a genuinely bad rating. Confidence
stays high. `runs_ads` still adds on top. Otherwise:

```
volume  = min(volume_max (60),
              volume_at_strong (40) · log10(review_count + 1) / log10(review_strong + 1))
quality = rating ≥ 4.5 → 40 · 4.0–4.4 → 30 · 3.5–3.9 → 15 · below → 0   (rating_bands)
viability = volume + quality  (+ runs_ads (10) if Meta Pixel / Google Ads / GTM found), capped at 100
```

`review_strong` comes from the market (Serbia 50, UAE 100), so the market's
"strong" count always scores 40 on volume. Serbia: 10 reviews → 24, 50 → 40,
300 → 58. UAE: 10 → 21, 100 → 40, 1000 → 60. Reasons: "4.7★/212 reviews",
"runs ads".

Why it exists: v3 had no such signal. A 4.8★ practice with 300 reviews and a
bad site should beat a dormant one with 2 reviews and the same bad site.

## 7. Reach — the best single first-touch channel (0–100)

`pipeline/channels.best_channel(lead, market)` picks the first channel the
lead has, in the **market's** channel order, skipping the market's
`first_touch_excluded` channels and placeholder phones/emails. Drafting uses
the same function, so the score and the suggested channel can't drift apart.

```
reach = reach_by_rank[rank of best channel in market.channels]   # [100, 90, 80, 60, 40]
```

| Market | 100 | 90 | 80 | 60 | 40 |
| --- | --- | --- | --- | --- | --- |
| Serbia | Instagram | Facebook | Viber/Whatsapp (mobile `+3816…`) | Email | Call (landline) |
| UAE | Instagram | Facebook | LinkedIn | Email | Call |

UAE WhatsApp (`+9715…`) is listed last in the market's channels and is
excluded for first touch: it stays visible (`usable_channels`) but is never
the best channel. Numbers without a country code are normalized with the
market's dialing code before the mobile check. No channel → reach 0 → the
L0 "no usable contact channel" gate. Reason: "best channel: Instagram".

v3 gave email 50, phone 40 and social 10, even though social is the channel
used first.

## 8. Priority, tiers and confidence

```
priority = (weights.need (0.6) · need + weights.viability (0.4) · viability) × (0.5 + reach/200)
           reach 100 → ×1.0 · 90 → ×0.95 · 80 → ×0.9 · 60 → ×0.8 · 40 → ×0.7

tier     = A ≥ 65 · B ≥ 45 · C ≥ 25 · D below      (scoring.tiers)
```

Then the caps, which only ever lower a tier:

- `niche_match == mismatch` (gate off) → at most **C**.
- `score_confidence == low` (`unclear`: `probe_failed` or `no_audit`) →
  at most **B**, plus the reason "needs manual look".

`score_confidence` is `high` / `low`, and low **only** when Need had no audit
data to rest on. A missing rating is a reason line, not low confidence.

Worked examples (Serbia, default config, computed with `score()`):

| Lead | Need | Viability | Reach | Priority | Tier |
| --- | --- | --- | --- | --- | --- |
| Audited site: no HTTPS, not mobile, © 2019, no booking/call, perf 30, SEO 60; 4.2★/30 reviews; mobile phone | 100 | 64.9 | 80 | (60 + 26) × 0.9 = **77.4** | A |
| No website, on Instagram, 4.7★/212 reviews | 100 | 94.5 | 100 | **97.8** | A |
| No website, email only, no rating data | 60 | 50 | 60 | (36 + 20) × 0.8 = **44.8** | C |

The thresholds are a first guess. The manifest records `tier_counts` for each
run: aim for A ≈ top 15–20% of a run and move `tiers` if a run is mostly A or
has almost none. One weight set for all lead types for now; split by
`lead_type` only if `score-report` shows one type behaving differently.

## 9. `score_reasons` and `audit_notes` — two fields, two readers

**`score_reasons`** (list of strings) explains the score to you: niche
match/mismatch, then the Need reasons, then the Viability reasons, then the
best channel, then "needs manual look" when confidence is low. Example:
`niche match · not mobile-friendly · no HTTPS · outdated (© 2019) ·
4.2★/30 reviews · best channel: Viber/Whatsapp`. Every number in the score
has a line behind it. It goes to Notion as `Score Reasons` (step 6) and to
`scored.csv`.

**`audit_notes`** is the fact sheet the draft prompt reads, rebuilt on every
scoring run by `_build_audit_notes()`: niche, location and country; the web
presence ("No website." / domain dead / certificate warning / didn't load /
"Has a website (x.rs): no HTTPS; not mobile friendly; copyright year 2019;
built on WordPress; …"); contact channels found. It holds **no ratings,
review counts, scores or Lighthouse numbers**: they must never reach a
message, and drafting never sees `score_reasons`.

## 10. Per-market values

From `config.yaml › markets.<id>` (the run's market, stamped in the manifest
at collect; `score --market` only for runs collected before markets existed):

| Value | Used in | Serbia | UAE |
| --- | --- | --- | --- |
| `country` | country gate | Serbia | United Arab Emirates |
| `exclude_name_patterns` | name gate | dom zdravlja, zavod, opšta bolnica, KBC, institut | (none yet) |
| `channels` (order) | Reach rank | Instagram, Facebook, Viber/Whatsapp, Email, Call | Instagram, Facebook, LinkedIn, Email, Call, WhatsApp |
| `first_touch_excluded` | Reach / channel gate | — | WhatsApp |
| `mobile_prefixes`, `dial_code` | mobile channel detection | `+3816`, `+381` | `+9715`, `+971` |
| `review_strong` | Viability volume | 50 | 100 |

A new market starts with empty `exclude_name_patterns` until you add some.
Everything else in `scoring:` (weights, Need points, rating bands, tiers) is
shared across markets.

## 11. Retuning

Calibration replaces guesswork (PIPELINE.md §7). `python -m pipeline.runner score-report
[--market X] [--niche Y] [--campaign Z] [--include-legacy] [--min-band N]` prints the tables below from
Notion + JSONL (read only); groups under `score_report.min_band` (30) are marked "not enough data".
Pages with `Campaign = legacy-v1` (the earlier niche and offer, docs/ROADMAP.md) are left out unless
`--include-legacy`; `--campaign Z` reports only that cohort (`legacy-v1` included when named):

- **L1 vs L2** (after ~50 drafts): send/skip rate by tier, `lead_type`,
  niche, market; `Skip Reason` counts. Healthy: skip rate rises A → C. A
  recurring `Skip Reason` is a missing gate (e.g. "chain" → lower
  `chain_listing_min`). If skip rate doesn't rise as tier drops, a sub-score
  weight is wrong.
- **L1 vs L3** (after ~30+ sends per band): reply and meeting rate by tier,
  market, channel, niche (sub-score bands: not in the report yet). Channel is
  the one on the latest `· SENT · <channel> ·` line in `Communication`. If most
  drafts get sent but replies are low, the message is the problem, not the score.
- **A/B rule** (strategy_gaps.md S19), for markets and for niches: the best arm
  by reply rate wins only with ≥ `ab_min_replies` (8) replies and
  ~`ab_ratio`× (2×) the runner-up's rate, and both arms at `min_band`+ sends;
  otherwise "not enough data" or "no clear difference".
- How pages count: **sent** = Status Sent / Follow-up due / Replied / Closed,
  `Last Contacted`, a SENT line or `Outcome` (Status Dead only next to one of
  the last three; sent beats `Verdict = Skip`). **Skipped** = `Verdict = Skip`,
  not sent. **Pending** = drafted, no decision (shown, not in rates). **Replied**
  = any `Outcome` but `no_reply`, Status Replied / Closed, or a REPLY line.
  **Meeting** = `Outcome` meeting / proposal / won.
- Don't retune on fewer than ~30 leads per band. Change one thing at a time,
  bump `scoring.version`, re-run `score`, then `sync --rescore` (step 6) so
  existing Notion pages update.

`scoring_version` is stamped on every scored lead from `scoring.version`.
Bump it whenever a weight, threshold or the formula shape changes, so old
and new scores are never compared as if they meant the same thing.

## 12. Testing

`score()` is pure — `tests/test_scoring.py` uses hand-built `Lead` objects
and a hand-built mirror of the config, no mocks:

- each L0 gate in order, including that a lead passing on re-score clears
  `disqualify_reason`
- niche match / unknown / mismatch, and the C cap vs the gate
- every `audit_case` branch, its `lead_type` and confidence
- Need points per item, the no-Lighthouse 80 max, `need_overrides`
- Viability volume per market `review_strong`, rating bands, no-data, ads
- Reach per market order, WhatsApp excluded in the UAE
- priority and tier for hand-computed leads, the B cap for low confidence
- D2: a bad redesign lead can outrank a weak new_build lead
- `audit_notes` holds facts only (no ratings, stars, scores, Lighthouse)
- `score()` doesn't mutate its input and leaves the v3 fields empty
- the shipped `config.yaml` matches the test mirror

## 13. Changelog

- **v5** (v2, after step 6) — a Maps listing with 0 reviews (rating 0 or
  empty) is "no reviews yet": viability = volume 0 + `no_reviews_quality`
  (20, new config key), reason "no reviews yet", confidence unchanged. In v4
  it fell into the rating path as a 0★ rating (viability 0) with the reason
  "0★/0 reviews", which dropped such leads a tier. Only Viability changes;
  everything else is as in v4.

- **v4** (schema v13, v2 step 5) — layered scoring replaces the single
  `total_score`. **L0 gates** (country = the run's market, closed, chain,
  excluded domain/category, per-market name patterns, optional niche
  mismatch, no first-touch channel) disqualify, keep the lead in JSONL only
  and record its key in `data/rejected_domains.json`. **L1**: Need /
  Viability / Reach → `priority` → tier A–D, with `score_reasons` and
  `score_confidence`; low confidence caps at B. Need scores visible problems
  (mobile, HTTPS, outdated, booking/tap-to-call) instead of
  `100 − Lighthouse`, and `unclear` leads get a provisional, flagged score
  instead of none. Viability (reviews, rating, ads) is new. Reach follows the
  market's channel order via `pipeline/channels.py` (social first) instead of
  email 50 / phone 40 / social 10. Per-market values: channel order and
  first-touch exclusions, mobile prefixes, `review_strong`, name patterns.
  `audit_notes` now holds facts only; scores and ratings moved to
  `score_reasons`. `reachability_score` / `opportunity_score` /
  `total_score` stay in the schema for one version, always empty, then go.
  - **Industry Fit removed.** There are no default niches (K1): each run's
    niche is created in `go`, so every lead in a run is in the same niche and
    weighting industries against each other means nothing. Replaced by the
    niche-match check (§3); niche learning moves to `score-report`.
  - **D2: "no website always outranks a bad audited site" dropped.** The v2
    rule `audited_score_cap < no_website_score` (config key, `config.py`
    validation and the scorer `assert`) is gone. With Instagram-only salons,
    "no site" isn't automatically the hotter lead: a business with a live but
    broken site, good reviews and a reachable owner can be the better
    prospect. `new_build` and `redesign` now compete on Need, and a bad
    redesign lead can outrank a weak new_build lead (tested).
- **v3** — dropped `icp_fit_score` and `intent_score` entirely. Industry and
  location moved out of scoring into hard qualification filters
  (`qualification:` in `config.yaml`), run before scoring rather than
  weighted into it. Added `reachability_score` (was folded into
  `icp_fit_score` before). Renamed `website_audit_score` →
  `opportunity_score` (same underlying redesign-case formula, unchanged
  values). `total_score` is now `opportunity_score × ` a
  reachability-derived multiplier instead of a three-way weighted sum. The
  `unreachable`/`unclear` case no longer gets a fixed discounted
  `opportunity_score` — it's left blank, flagged for manual
  classification, instead of guessing which pitch applies. Removed the
  manual intent-research workflow (`intent_score`, `research_rank()`,
  `research_pool.csv`) — dropped along with `icp_fit_score`'s removal
  rather than kept as a disconnected leftover feature.
- **v2** — capped the audited-site computed score below `no_website_score`
  (`audited_score_cap`, default 85) so a bad audited site can never outrank
  a lead with no website at all (reversed in v4, D2); changed the
  established-practice bonus to require distinct channel *types* rather than
  a raw item count; made the manual intent-research pool size explicit and
  configurable.
- **v1** — initial weighted scoring: `icp_fit` (contact channels + industry
  match), `website_audit` (Lighthouse + https/mobile flags, fixed scores for
  no-website/unreachable), manual `intent_score`, `lead_type`.
