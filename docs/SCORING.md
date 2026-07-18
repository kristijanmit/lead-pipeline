# AGENCY lead scoring — design notes

## 1. Overview

`pipeline/scoring/scorer.py` implements
`score(lead, weights, qualification) -> Lead`, a pure function with no I/O
(per ARCHITECTURE.md §6/§13). This document is both the "why" behind the
model — what the qualification filters are gating, what each sub-score is
trying to measure, why they're kept separate, how the edge cases in real
data are handled — and an implementation reference, section by section:

| Section | Concept | Function in `scorer.py` |
| --- | --- | --- |
| §2 | Qualification gate | `_qualifies()` |
| §4 | `reachability_score` | `_reachability_score()` |
| §5.1 | Which audit case a lead falls into | `_audit_case()` |
| §5.2 | `opportunity_score` (audited case) | `_opportunity_score()` |
| §6 | `total_score` multiplier | `_reachability_multiplier()` |
| §7 | `Audit Notes` | `_build_audit_notes()` |
| §8 | `lead_type` | shares `_audit_case()` with §5.1 |

How to retune the formula once real reply data exists is covered in
`docs/ROADMAP.md`'s Phase 3/6 tuning items.

## 2. Qualification filters — a gate, not a score

Before anything is scored, a lead has to pass two hard filters
(`qualification:` in `config.yaml`, resolved by `_qualifies()` in
`scorer.py`):

- **Industry** — the raw collected `industry` value (a free-form string from
  OSM tags / maps categories / manual CSV input) must resolve, via
  `qualification.industry_map`, to one of the target categories (`clinic`,
  `car_workshop`, `workshop_repair`, `construction` by default). The
  resolution is case-insensitive and never raises — an unrecognized raw
  value simply fails the filter, since the raw string can be anything.
- **Country** — `lead.country` (stamped at collection time, the same way
  `location` is) must be in `qualification.target_countries`.

If a lead fails either filter, `reachability_score`, `opportunity_score`,
and `total_score` are all left `None`, `qualified` is `False`, and
`Audit Notes` states which filter failed instead of a score breakdown.
Nothing downstream computes a score for a lead outside scope.

**Why filters and not weights:** it's too early to know which of these
verticals actually converts — weighting them against each other now would
be guessing at data that doesn't exist yet. Once there's real close-rate
data across verticals, industry weighting can come back as a deliberate,
evidence-based scoring dimension, not folded back into a hard gate.
Deliberately **not** reintroduced here: industry weighting, business-size
signals, or multi-location logic — all removed on purpose pending that data
(see the v3 changelog in §10).

A lead that passes both filters has its `industry` folded to the canonical
category (e.g. a raw `dentist` becomes `clinic`) — this is what actually
syncs to Notion's `Industry` column, not the raw collected string.

## 3. The core split: reachability vs. opportunity

Two different questions, kept as separate sub-scores so a high or low total
is always explainable, rather than averaged into one number that looks the
same whether a lead can't be reached or just has a good website:

| Score               | Question it answers                                          | What moves it                                                       |
| -------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `reachability_score` | Can we actually contact this lead right now, today?           | Which contact channels are present (email, phone, social)              |
| `opportunity_score`  | How much upside is there in pitching a redesign or new build? | How bad (not how good) their web presence is — runs in the "pain" direction |
| `lead_type`          | Which pitch applies — new build, redesign, or unclear?         | Same branch as `opportunity_score`, see §8; doesn't feed `total_score` directly |
| `total_score`        | Overall outreach priority among qualified leads                | `opportunity_score × ` a multiplier derived from `reachability_score`  |

A dentist with a fast, accessible, mobile-friendly WordPress site and no
listed phone number scores low on both — nothing to sell, and no way to
reach them even if there were. A dentist with no website at all but three
phone numbers and an Instagram scores high on both — reachable, and in
obvious need of a web presence.

## 4. `reachability_score` — can we contact them, today

```
has_email  = Primary Email is non-empty   -> +50
has_phone  = Phone is non-empty            -> +40
has_social = Social Links is non-empty     -> +10

reachability_score = has_email*50 + has_phone*40 + has_social*10
```

Deliberately does **not** count having a `domain`/website as a contact
channel — a website isn't itself a way to reach someone, only a signal that
one might exist somewhere on it. (This is a real difference from the
enrich-stage `_has_no_contact_info` filter in `runner.py`, which *does*
treat `domain` as a reason to keep a lead — that filter runs earlier, for a
different purpose: dropping leads with literally nothing to act on at all.
A lead with only a domain and no email/phone/social passes that filter but
still gets `reachability_score == 0` here.)

If `reachability_score == 0`, the lead is never marked `qualified` — there's
no channel to act on yet — but it is still scored (see §6 on the total-score
multiplier at `reachability_score == 0`) rather than being silently dropped;
`Audit Notes` flags it as needing further enrichment.

## 5. `opportunity_score` — opportunity, not quality

This is the inverse of a normal website-quality score. A high
`opportunity_score` means *more* opportunity to sell into, not a healthier
site. Computed differently depending on `lead_type`:

### 5.1 The three cases

| Case                                                 | `lead_type`  | `opportunity_score`                                      |
| ------------------------------------------------------ | ------------ | ----------------------------------------------------------- |
| **No domain at all**                                  | `new_build`  | Fixed policy value, `opportunity.no_website_score` (default 90) |
| **Domain exists, unreachable during audit**            | `unclear`   | `None` — left blank, `Audit Notes` flags "needs classification" |
| **Domain exists, audited**                             | `redesign`   | Computed from Lighthouse + flags, capped at `audited_score_cap` (default 85) |

An `unclear` lead genuinely doesn't tell you which pitch applies — the
domain could be a live business with a site that's temporarily down
(a redesign pitch) or an abandoned domain (arguably no pitch yet). Rather
than default to either the new-build or a discounted fixed value (as an
earlier version of this model did), it's left blank on purpose — a `None`
here means "needs a manual look," not "zero opportunity."

`no_website_score` (90) is a fixed policy value, not a placeholder: a
business with zero web presence is assumed to have high need until evidence
says otherwise for a given vertical.

### 5.2 The computed case (redesign leads)

Unchanged from the previous scoring model — only the field name changed
(`website_audit_score` → `opportunity_score`):

```
weighted_quality = Σ (lighthouse[metric] × lighthouse_weight[metric])
opportunity       = 100 − weighted_quality
opportunity      += no_https_bonus            if https is False
opportunity      += not_mobile_friendly_bonus if mobile_friendly is False
opportunity       = min(opportunity, audited_score_cap)
```

If Lighthouse returned only some categories, the weights are renormalized
over the categories present — a missing category reads as "no signal," not
fake opportunity. If *none* of the weighted categories are present, there's
nothing to compute from and the lead falls into the `unclear` case above.
The result is clamped to `[0, 100]` so the flat bonuses can't push it past
the scale.

**The cap:** without it, a terrible audited site (very low Lighthouse
quality plus both bonuses) can compute to 100 — beating `no_website_score`'s
fixed 90. That's backwards: a lead with no website at all is the strongest
new-build signal in the batch, and a bad audited site is still a warmer,
easier sell than a blank slate — there's already an owner who plausibly
knows they need a working site, no domain/DNS setup required, and a live
baseline to point at. `audited_score_cap` (default 85) enforces that by
construction: it must always stay below `no_website_score`, and both
`config.py`'s startup validation and an assertion in `scorer.py` enforce
that ordering.

Default Lighthouse weights (unchanged from the previous model):

| Metric           | Weight | Why                                                                       |
| ----------------- | ------ | ---------------------------------------------------------------------------- |
| `performance`    | 0.40   | Most visible, most correlated with "this site feels broken" to a visitor    |
| `best_practices` | 0.25   | Catches structural/security issues perf alone misses                        |
| `accessibility`  | 0.20   | Real gap, but rarely the reason a lead replies to cold outreach             |
| `seo`            | 0.15   | Weighted lowest — most sites in this dataset already score 75–100 here |

`https is False` and `mobile_friendly is False` add flat bonuses on top,
since Lighthouse doesn't always penalize these cleanly and they're easy,
concrete talking points in a pitch.

## 6. `total_score`

```
multiplier =
    1.0  if reachability_score == 100
    0.95 if reachability_score == 90
    0.7  if 40 <= reachability_score <= 89
    0.5  if 1 <= reachability_score <= 39
    0.2  if reachability_score == 0

total_score = opportunity_score * multiplier
```

`total_score` is `None` whenever `opportunity_score` is `None` (the
`unclear` lead-type case) — there's nothing to multiply. The multiplier is
a fixed policy table in `scorer.py`, not a tunable config weight — unlike
the point values in `reachability_score`, these bucket boundaries are a
deliberate business rule, not something to retune per batch.

Note the `reachability_score == 0` row still produces a (small) total
rather than `None` — the lead isn't marked `qualified` (see §2), but if that
gate is ever bypassed downstream, a `0.2` multiplier keeps it from ranking
artificially high rather than crashing on a missing value.

`scoring_version` is stamped on every scored `Lead` from `weights["version"]`
(ARCHITECTURE.md §5). Bump it whenever the weights *or formula shape*
change, not just the numbers, so old and new `total_score`s are never
silently compared as if they meant the same thing.

## 7. `Audit Notes` — every score explainable

Auto-generated on every scoring run by `_build_audit_notes()` in
`scorer.py`, overwriting whatever `enrichers/audit.py` wrote at enrich time.
Three shapes, depending on outcome:

- **Qualified**: `"Qualified: {industry}, {country}. Opportunity: {source}
  ({value}). Reachability: {breakdown} ({value}, x{multiplier} multiplier).
  Total: {total}."` — e.g. `"Qualified: clinic, Serbia. Opportunity:
  new-build policy (90). Reachability: phone only (40, x0.7 multiplier).
  Total: 63."`
- **Passes filters but zero reachability**: same shape, `"Not qualified (no
  contact info — needs further enrichment): ..."` in place of `"Qualified:
  ..."`.
- **Fails a qualification filter**: `"Not qualified: industry '{industry}'
  not in target list"` / `"country '{country}' not in scope"` (or both,
  joined with `; `) — no score breakdown, since none was computed.

## 8. `lead_type` — new-build vs. redesign are different pitches

Set by `scorer.py` from the exact same branch that decides
`opportunity_score`, via a single shared helper (`_audit_case`) so the two
can never disagree about which case a lead falls into:

| `_audit_case` result | `opportunity_score`              | `lead_type`  | Meaning                                                      |
| --------------------- | ---------------------------------- | ------------ | -------------------------------------------------------------- |
| `"no_website"`        | fixed `no_website_score`          | `new_build`  | No domain at all — the pitch is building a site from scratch  |
| `"unreachable"`       | `None`                            | `unclear`   | Domain exists but couldn't be audited — genuinely ambiguous until someone looks |
| `"audited"`           | computed from Lighthouse + flags   | `redesign`  | Domain exists and was audited — the pitch is improving an existing site |

This stays a separate field rather than folding into `total_score` itself —
two leads can tie on `total_score` and still need different outreach
templates. `unclear` leads are worth a specific manual look before either
pitch goes out — sending a "let's redesign your site" pitch to a business
whose domain is actually dead would land badly.

## 9. Testing

Per ARCHITECTURE.md §13, `score()` is a pure function — test with
hand-built `Lead` objects, no mocking (see `tests/test_scoring.py`):

- Industry/country filter failures leave every score field `None` and
  `qualified=False`, and never raise on an unrecognized raw industry string
- A qualifying industry synonym (e.g. `dentist`) folds `lead.industry` to
  its canonical category (`clinic`)
- Every reachability channel combination produces the documented score and
  multiplier bucket
- `reachability_score == 0` still produces a `total_score` (via the `0.2`
  multiplier) but `qualified` stays `False`
- A no-domain lead → `opportunity_score == no_website_score`
- An `enrich_failed`/unreachable-domain lead → `opportunity_score is None`,
  `Audit Notes` flags "needs classification"
- A fully-audited lead with known Lighthouse numbers → hand-computed
  `weighted_quality` matches
- `lead_type` and `opportunity_score` always agree on which case a lead fell
  into — both derive from the single `_audit_case()` branch
- A terrible audited site never exceeds `audited_score_cap`, which stays
  below `no_website_score` — both directly, and via an assertion that a
  misconfigured `audited_score_cap >= no_website_score` raises
- `Audit Notes` matches the documented format exactly for at least one
  hand-computed example
- Two leads with identical inputs except `weights["version"]` produce
  different `scoring_version` on output, identical everything else

## 10. Changelog

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
  a lead with no website at all; changed the established-practice bonus to
  require distinct channel *types* rather than a raw item count; made the
  manual intent-research pool size explicit and configurable.
- **v1** — initial weighted scoring: `icp_fit` (contact channels + industry
  match), `website_audit` (Lighthouse + https/mobile flags, fixed scores for
  no-website/unreachable), manual `intent_score`, `lead_type`.
