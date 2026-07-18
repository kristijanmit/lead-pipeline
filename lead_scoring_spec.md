# Lead scoring spec — ONIX Leads Pipeline

**Owner:** Enrichment/scoring pipeline (all fields in this spec are written by enrichment only — see field-ownership notes at the end)
**Status:** Draft for review, replaces the current blended ICP Fit Score / Website Audit Score logic

## Why this changed

The current ICP Fit Score blends how well a business matches the target customer with whether we happen to have a way to contact them — that's why no-website leads ranged 15–85 with no explanation. Website Audit Score also did two different jobs depending on lead type (inverted quality score for redesigns, flat placeholder for new-builds) with no note distinguishing the two.

This version splits scoring into two independent, explainable numbers — **Reachability** and **Opportunity** — and drops ICP Fit Score entirely for now. Industry and location become hard qualification filters instead of scored inputs: it's too early to know which of clinics / car workshops / other workshops / construction actually convert best, so weighting them now would just be guessing. Once you have real close-rate data across verticals, industry weighting can be reintroduced as a deliberate, evidence-based step — not before.

---

## 1. Qualification filters (not scored)

Before any scoring happens, a lead must pass these gates to be eligible at all:

```
industry_in_target = Industry is one of: clinic, car_workshop, workshop_repair, construction
location_in_scope  = business operates in Serbia
```

Both are binary — a lead either qualifies for the pipeline or it doesn't. Neither affects ranking among qualified leads. If a lead fails either filter, it should not be scored or entered into the board at all (or entered but never marked `Qualified` for outreach handoff).

This replaces the old approach where location/industry contributed to a score. Once you're ready to weight industries against each other (e.g. clinics converting better than workshops), that becomes a new, explicit scoring dimension — not folded back into this filter.

---

## 2. Reachability Score (0–100)

**What it measures:** can we actually contact this lead right now, today.

**New field:** `Reachability Score` (number)

```
has_email   = Primary Email is non-empty                → +50
has_phone   = Phone is non-empty                          → +40
has_social  = Social Links is non-empty                   → +10

Reachability Score = has_email*50 + has_phone*40 + has_social*10
```

| Situation              | Score |
| ---------------------- | ----- |
| Email + phone + social | 100   |
| Email + phone only     | 90    |
| Phone only             | 40    |
| Email only             | 50    |
| Nothing                | 0     |

**Threshold:** leads with `Reachability Score == 0` should not be marked `Qualified` — there's no way to act on them yet. Route these back for further enrichment rather than dropping them silently.

---

## 3. Opportunity Score (renamed from `Website Audit Score`)

**What it measures:** how much upside there is in pitching this lead a new site or redesign. Computed differently depending on `Lead Type`.

**Rename field:** `Website Audit Score` → `Opportunity Score` (same underlying property, just relabeled so it isn't mistaken for a quality score)

**Redesign leads** (has a site — keep current logic, just document it):

```
Opportunity Score = 100 minus weighted_avg(
    Performance Score   x 0.35,
    Accessibility Score x 0.20,
    Best Practices Score x 0.20,
    SEO Score            x 0.25
)
```

A bad site gives high opportunity. A great site gives low opportunity.

**New-build leads** (no site):

```
Opportunity Score = 90   (fixed policy value)
```

This is an explicit design decision, not a placeholder: a business with zero web presence is assumed to have high need. Revisit if evidence says otherwise for a given vertical.

**Unclear lead type:** if `Lead Type = unclear`, leave `Opportunity Score` blank and flag the row in Audit Notes as "needs lead-type classification" rather than defaulting to either path.

---

## 4. Total Score

```
Total Score = Opportunity Score x Reachability Multiplier
```

Where:

| Reachability Score | Multiplier                                                                                               |
| ------------------ | -------------------------------------------------------------------------------------------------------- |
| 100                | 1.0                                                                                                      |
| 90                 | 0.95                                                                                                     |
| 40-89              | 0.7                                                                                                      |
| 1-39               | 0.5                                                                                                      |
| 0                  | 0.2 (should already be excluded from Qualified, but keeps the row from ranking high if it slips through) |

With ICP Fit removed, Total Score is purely "how much upside, adjusted down if we can't easily reach them." Ranking within the qualified pool is now driven entirely by opportunity + reachability — industry/vertical no longer influences the ranking, only whether a lead is in the pool at all.

---

## 5. Audit Notes — make every score explainable

Auto-generate a one-line breakdown into `Audit Notes` whenever scores are computed, e.g.:

```
Qualified: clinic, Serbia. Opportunity: new-build policy (90). Reachability: phone only (40, x0.7 multiplier). Total: 63.
```

---

## 6. Migration for existing 44 rows

1. Backfill `Reachability Score` for all existing rows from current Primary Email / Phone / Social Links.
2. Remove `ICP Fit Score` from the schema (or archive it, don't delete history if you want to compare later). Stop writing to it.
3. Rename `Website Audit Score` to `Opportunity Score`; no value changes needed for redesign leads, just relabel. New-build leads at 90 stay as-is.
4. Recompute `Total Score` for all rows using the new two-factor formula.
5. Re-run the qualification filter against the expanded industry list (clinic / car_workshop / workshop_repair / construction) and Serbia-wide location scope — this will change which of the current 44 Novi Sad dentists remain "in scope" (all should, since dentist maps to clinic) and opens the door for new verticals going forward.
6. Backfill `Audit Notes` with the explainability string for at least the top 20 by new Total Score.

---

## 7. Field ownership

`Reachability Score`, `Opportunity Score`, `Total Score`, `Audit Notes`, and the qualification filter fields are all written exclusively by the enrichment pipeline. The outreach pipeline reads them but never writes to them, including on re-runs.

## 8. Schema changes needed

- Remove `ICP Fit Score` property (or archive).
- Rename `Website Audit Score` to `Opportunity Score`.
- Industry: add options `clinic`, `car_workshop`, `workshop_repair`, `construction` (fold existing `dentist` rows under `clinic`, or keep as a sub-tag if you want that granularity later).
- Location: currently a select with only "Novi Sad" / "Testville". With Serbia-wide scope, switch to free-text city (or an expanding select), and treat it purely as a qualification filter, not a scored field.
