# AGENCY lead scoring — design notes

## 1. Overview

`pipeline/scoring/scorer.py` implements `score(lead, weights) -> Lead`, a
pure function with no I/O (per ARCHITECTURE.md §6/§13). This document is the
"why" behind the weights — what each sub-score is trying to measure, why
they're kept separate, how the edge cases in real data are handled, and how
to retune the formula once real reply data exists.

## 2. The core split: fit vs. opportunity

The single biggest way lead scoring goes wrong for an agency is conflating
"is this a good business to work with" and "how good is their website."
These are different questions, and mixing them into one number hides which
lever actually explains why a lead scored high or low.

| Score                 | Question it answers                                          | What moves it                                                  |
| ---------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------- |
| `icp_fit_score`       | Is this a real, reachable business worth pursuing?            | Contact channels present, signs of an established practice     |
| `website_audit_score` | How much opportunity does this lead represent for our services? | How bad (not how good) their web presence is — this runs in the "pain" direction |
| `intent_score`        | Has someone manually confirmed this lead is worth prioritizing right now? | Nothing automatic — set by hand, see §5 |
| `lead_type`           | Which pitch applies — new build, redesign, or unclear?         | Same branch as `website_audit_score`, see §8; doesn't feed `total_score` |
| `total_score`         | Overall outreach priority                                      | Weighted sum of the three scores above, weights in `config.yaml` |

A dentist with a fast, accessible, mobile-friendly WordPress site and no
listed phone number should score *low* on `icp_fit` (can't reach them) but
would also score low on `website_audit` (nothing to sell). A dentist with no
website at all but three phone numbers and an Instagram should score *high*
on both — reachable, and in obvious need of a web presence. Keeping the two
scores separate makes both of those cases legible instead of averaging them
into a mediocre single number that looks the same either way.

## 3. `icp_fit_score` — reachability and legitimacy

Deliberately does not look at `lighthouse`, `https`, `mobile_friendly`, or
`cms` at all. Those describe the website; this describes the business.

Components (see `icp_fit` weights in `config.yaml`):

- **Has at least one email** — a channel for outreach that isn't a phone
  call.
- **Has at least one phone** — same reasoning, other direction. Many small
  dental practices in the OSM data (e.g. "Neskovic Dent", "Estetika") have a
  phone and nothing else — that's still a workable lead.
- **Has at least one social link** — signals an actively maintained public
  presence, not just a stale directory listing.
- **Established-practice bonus** — triggered when the combined count of
  emails + phones + socials crosses a threshold (default: 2). More contact
  surface tends to mean a multi-person practice with a bigger budget and
  someone who plausibly owns "fix the website" as a task, rather than a
  solo practitioner running everything themselves.
- **Industry match** — currently full credit, since collection is
  single-industry per run (see ARCHITECTURE.md §2, non-goals). This becomes
  a real filter once a run mixes industries; kept as a named field now
  rather than added later as a schema change.

`icp_fit_score` intentionally ignores company size proxies beyond contact
count (headcount, revenue) — that data isn't in the `Lead` schema and isn't
collected for free, so the score only uses what's actually available rather
than a proxy that would silently be wrong.

## 4. `website_audit_score` — opportunity, not quality

This is the inverse of a normal website-quality score. High
`website_audit_score` means *more* opportunity to sell into, not a healthier
site.

### 4.1 The three cases

| Case                                                            | Example from real data                                      | Score                                    |
| ------------------------------------------------------------------ | ---------------------------------------------------------- | ----------------------------------------- |
| **No domain at all**                                             | "Веселин", "Рисус", "Otro4u"                                | Fixed `no_website_score` (default 90)     |
| **Domain exists, unreachable during audit**                      | "Komnenović" (`stomatolognovisad-komnenovic.in.rs`), "Pro Dental Solution" | Fixed `unreachable_domain_score` (default 65) |
| **Domain exists, audited**                                        | "Bobić", "Markov Dental", "Prosmile"                        | Computed from Lighthouse + flags (below)  |

The no-domain and unreachable-domain cases exist because
`lead.lighthouse is None` in both, and letting that fall through to a
computed formula would silently zero them out — the worst-looking outcome
for what are actually two of the strongest opportunity signals in the
dataset. They're scored differently from each other (90 vs. 65) because a
domain that fails to resolve or connect carries a real chance the practice
has gone under or the domain was abandoned — worth a discount versus a
confirmed-live business with zero web presence.

These two cases are flagged as a fixed score, deliberately, rather than
computed — there's no Lighthouse data to compute *from*. Note this also
means they represent a different pitch (new build vs. redesign) than the
computed case below; see §8 for how each case maps to `lead_type`.

### 4.2 The computed case

For an audited site:

```
weighted_quality = Σ (lighthouse[metric] × lighthouse_weight[metric])
opportunity       = 100 − weighted_quality
opportunity      += no_https bonus            if https is False
opportunity      += not_mobile_friendly bonus if mobile_friendly is False
```

(In the implementation, if Lighthouse returned only some categories, the
weights are renormalized over the categories present — a missing category
reads as "no signal", not as fake opportunity. If *none* of the weighted
categories are present, there is nothing to compute from and the lead falls
back to the unreachable case above. The result is clamped to [0, 100] so
the flat bonuses can't push a sub-score past the scale.)

Default Lighthouse weights:

| Metric          | Weight | Why                                                                 |
| ---------------- | ------ | -------------------------------------------------------------------- |
| `performance`   | 0.40   | Most visible, most correlated with "this site feels broken" to a visitor |
| `best_practices` | 0.25   | Catches structural/security issues perf alone misses                 |
| `accessibility` | 0.20   | Real gap, but rarely the reason a lead replies to cold outreach       |
| `seo`           | 0.15   | Weighted lowest — most sites in this dataset already score 75–100 here, so it has the least room to differentiate leads |

`https is False` and `mobile_friendly is False` add flat bonuses on top of
the Lighthouse-derived score, since Lighthouse doesn't always penalize these
cleanly and they're easy, concrete talking points in a pitch ("your site
isn't mobile-friendly" is a one-sentence hook that a Lighthouse performance
number isn't).

## 5. `intent_score` — manual, never computed

Per the Phase 3 roadmap item ("Add a manual Intent Score workflow: quick
LinkedIn/news check on your top 20 candidates before final ranking, entered
by hand"), `scorer.py` never writes to this field — it only ever passes
through whatever value is already on the `Lead`. This is deliberate:

1. Run `score()` once with `intent_score` at its default (`0.0`) to get an
   initial ranking from `icp_fit` and `website_audit` alone.
2. Manually research the top ~20 by `total_score` — recent activity,
   reviews, anything suggesting they're actively looking to invest right
   now.
3. Hand-edit `intent_score` on those leads in the JSONL (or a review sheet),
   then re-run `score()` — `total_score` updates, `icp_fit_score` and
   `website_audit_score` are recomputed identically since they don't depend
   on `intent_score`.

In practice: edit `intent_score` directly in `scored.jsonl` and re-run
`python -m pipeline.runner score --run <run_id>` — the score command carries
non-zero `intent_score` values forward from the existing `scored.jsonl`
before rescoring, so hand-entered intents survive re-runs.

Default weight on `intent` in `total_score` is intentionally the smallest of
the three (0.15) — it's `0.0` for every lead until step 2 happens, so giving
it too much weight would just flatten most of the batch to the same
baseline.

## 6. `total_score` and versioning

```
total = icp_fit_score × weights.total.icp_fit
      + website_audit_score × weights.total.website_audit
      + intent_score × weights.total.intent
```

Default weights: `icp_fit` 0.35, `website_audit` 0.50, `intent` 0.15 —
opportunity weighted highest since that's the strongest, most-available
signal across the whole batch; fit next since it gates whether outreach is
even possible; intent last since it's sparse by design.

`scoring_version` is stamped on every scored `Lead` from `weights["version"]`
(ARCHITECTURE.md §5 — this is what makes a `total_score` of 62 from three
weeks ago comparable, or not, to one from today). Bump it whenever the
weights or formula shape change, not just the numbers — a weight change
alone should also bump the version so old and new scores are never silently
compared as if they meant the same thing.

## 7. Tuning after a real batch

Per the Phase 3 roadmap: don't guess the weights twice. After the first
batch of real outreach:

1. Track which leads actually replied (a column in the review sheet or
   Notion, not a new `Lead` field — this is outreach-outcome data, not
   pipeline data).
2. Compare `icp_fit_score` and `website_audit_score` distributions between
   repliers and non-repliers. If repliers cluster on one sub-score more
   than the other, shift `weights.total` toward it.
3. Check the two fixed-score cases (`no_website_score`,
   `unreachable_domain_score`) against reply rate specifically — if
   no-website leads reply worse than expected, that's a sign the "sell a
   new build" pitch needs work, not that the score is wrong; resist the
   urge to fix a pitch problem by editing the score.
4. Re-check `lighthouse_weights` — if `performance` isn't actually the thing
   that lands in conversations, move weight toward whichever metric is.

## 8. `lead_type` — new-build vs. redesign are different pitches

Resolved: added `Lead.lead_type: str | None`, one of `"new_build"` /
`"redesign"` / `"unclear"` (schema bump to v4 in `pipeline/schema.py`).
Set by `scorer.py` from the exact same branch that decides
`website_audit_score`, via a single shared helper (`_audit_case`) so the
two can never disagree about which case a lead falls into:

| `_audit_case` result | `website_audit_score`         | `lead_type`  | Meaning                                                      |
| --------------------- | ------------------------------ | ------------ | -------------------------------------------------------------- |
| `"no_website"`       | fixed `no_website_score`      | `new_build`  | No domain at all — the pitch is building a site from scratch  |
| `"unreachable"`      | fixed `unreachable_domain_score` | `unclear`  | Domain exists but couldn't be audited — genuinely ambiguous until someone looks: could be a live business with a site that's temporarily down (redesign/fix pitch), or an abandoned domain (no real pitch yet). Deliberately not guessed either way. |
| `"audited"`          | computed from Lighthouse + flags | `redesign` | Domain exists and was audited — the pitch is improving an existing site |

This stays a separate field rather than a change to `total_score` itself,
per the original framing — two leads can tie on `total_score` and still
need different outreach templates. In practice this means the outreach
step (or a future `sinks/` consumer) can filter or branch on `lead_type`
without re-deriving it from `domain`/`lighthouse`/`status` each time.

`unclear` leads are worth a specific note: they should probably get a
manual look (same spirit as the intent-score workflow in §5) before either
pitch goes out, rather than being routed automatically — sending a
"let's redesign your site" pitch to a business whose domain is actually
dead would land badly.

Migration note: any v3 JSONL loaded without a `lead_type` key gets
`lead_type=None` via the dataclass default — no explicit migration code
needed (same pattern as the v2→v3 `contact_phone` migration in
ARCHITECTURE.md §5), since `None` is exactly the right value for "not yet
scored under this schema version." Re-running `score()` on old records
fills it in.

## 9. Testing

Per ARCHITECTURE.md §13, `score()` is a pure function — test with
hand-built `Lead` objects, no mocking (see `tests/test_scoring.py`):

- A no-domain lead → `website_audit_score == no_website_score`
- An `enrich_failed` lead → `website_audit_score == unreachable_domain_score`
- A fully-audited lead with known Lighthouse numbers → hand-compute the
  expected `weighted_quality` and assert against it
- `intent_score` on the input is always equal to `intent_score` on the
  output — the one invariant that must never break
- Two leads with identical inputs except `weights["version"]` produce
  different `scoring_version` on output, identical everything else
- `lead_type` and `website_audit_score` always agree on which case a lead
  fell into — e.g. a lead can never come back `lead_type="new_build"` with
  a `website_audit_score` computed from Lighthouse data, since both are
  derived from the single `_audit_case()` branch
