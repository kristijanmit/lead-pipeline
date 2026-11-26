# Lead pipeline v2 vs ONIX project docs — gaps and decisions

Checked: `PIPELINE.md` v2 and `script_edits.md` (code-checked at `7dc42c6`) against the ONIX project docs
(`uae-clinic-lane.md`, `website-analysis-2026-11-23.md`, `website-edits-2026-11-23.md`) and the repo's
`config.yaml`, `config/outreach.md` and drafter.

## Decisions (2026-11-26)

| # | Decision | Effect |
| --- | --- | --- |
| **K1** | **No default niches.** A niche is always created in `go` (or spelled out in full in a batch entry). Nothing in config or code names a niche. | Remove `collect.categories`, `outreach.industry_labels` (8 niches hard-coded in `config.py`), the "unknown category" warning and the niche-specific example sections as defaults. Industry Fit weighting goes away (see §B). |
| **K2** | **Handle English and non-local markets, the UAE included.** | A **market** profile (country, phone rules, language, channel order, currency…) is chosen or created in `go` and stamped on every lead. Drafting gets an English rule set next to the Serbian one. |
| **K3** | **Agency lane removed completely for now.** | No agency scoring profile, no `Lane` field, no LinkedIn intake, no kill-rule report. Lead identity is Market + Niche. |

Superseded from the earlier version of this doc: S1/S2 (target-niche gate in config, Fit by niche), S7/S20 (agency),
the `Lane` field, decision 3. Earlier `script_edits.md` D1 (empty `industry_fit` map) is replaced by K1.

---

## A. How a run is defined now (`go`)

```
go
 ├─ Mode: Lead Gen / Draft Outreach
 ├─ Market: pick a configured one (e.g. Serbia, UAE) or "new market"
 │     new market asks: country · dialing code · mobile prefixes · language (sr / en)
 │                      · channel order · currency · Maps language → saved to the run (and offered as YAML to paste into config)
 ├─ Niche (always typed, never preselected):
 │     name                      e.g. "dental clinic" / "stomatološka ordinacija"
 │     search terms              terms sent to Maps/OSM (default: the name)
 │     draft phrase              plural phrase in the market language, e.g. "stomatološke ordinacije" / "dental clinics"
 │     accepted Google categories (optional) — used for the niche-match check; empty = no check
 │     question for this niche   (optional) — otherwise a generic question is used
 │     landing URL               (optional) — e.g. onixweblab.com/ordinacije or /clinics, used only if links are turned on
 ├─ Location: city or area (bbox from maps.areas, or typed)
 └─ Confirm → collect → enrich → score → sync
```

- The niche and market definitions are written into the run's `manifest.json` and onto every lead (`market`,
  `language`, `niche`, `niche_label`). **They are synced to Notion** (`Market`, `Language`; the niche name goes into the existing `Industry` select), because
  `draft-notion` works from Notion pages, not from runs, and needs them to draft.
- Optional convenience: `go` may list niches from earlier runs as "copy from run X". You pick explicitly, and nothing is
  preselected. Drop this if you'd rather type it every time.
- `batch` YAML: each entry carries the full `market` and `niche` blocks. There's no lookup and no default.
- **Markets vs niches:** market presets (Serbia, UAE) can live in `config.yaml` because they're facts about a country, not
  business choices. Grid areas (bboxes) are geography and stay in config too. UAE boxes for Dubai, Sharjah, Ajman, Abu Dhabi and Al Ain are in `script_edits.md` 10.4 (approximate, VERIFY on a map).

## B. Scoring changes caused by K1/K2

- **Fit by industry is removed.** Every lead in a run belongs to the niche you chose, so ranking niches against each other
  makes no sense inside a run. It's replaced by a **niche-match check**:
  - Maps category is in the niche's accepted categories → match.
  - No accepted categories given, or no Maps category (OSM, manual) → unknown, no effect.
  - Category clearly different → `niche_mismatch`. That's an L0 gate if you turn on `gates.niche_mismatch: true`, otherwise a reason line and tier capped at C.
- **Priority** = `(0.6·need + 0.4·viability) × (0.5 + reach/200)`. Same tiers. `score-report` can still show Go-rate **by niche**
  across runs. That's where niche learning happens, outside the formula.
- **Per-market values** (from the market profile, not hard-coded):
  - Reach values follow the market's channel order. A channel the market forbids for first touch (UAE: WhatsApp) scores 0 for reach, but it stays visible.
  - Viability review band: Serbia "strong" from 50 reviews, UAE from 100 (from the UAE doc and website-analysis §4.2).
  - Mobile detection: Serbia `+3816…`, UAE `+9715…`. Numbers without a country code are normalized with the market's dialing code first.
- Need, gates (country = the run's market country, closed, chain, public-institution name patterns, no channel) and tiers are otherwise as in `script_edits.md`.

## C. English / non-local market — what has to change

| Area | Today (code) | Change |
| --- | --- | --- |
| Country | `collect.country: Serbia` default + `qualification.target_countries: [Serbia]` (`config.py:25,104`) | Country comes from the run's market. Validation no longer requires a config default. |
| Channels | `channel.py`: Serbian mobile check, labels `Viber/Whatsapp`, order fixed | Shared `pipeline/channels.py` takes the market: its mobile prefixes, its channel list and order, its first-touch exclusions. Labels per market (SR "Viber/Whatsapp", UAE "WhatsApp"). |
| Drafting rules | `drafter.py`: Serbian system prompt, Serbian banned phrases, greetings "Pozdrav"/"Dobar dan", Cyrillic check, "Vi" form | Split into **rule sets per language**: `drafting/rules_sr.py`, `drafting/rules_en.py`. Each has its system prompt, banned phrases, greeting per channel, script check (SR: no Cyrillic; EN: no Arabic or Cyrillic) and address form. `validate_message(message, channel, rules)`. |
| English banned phrases | none | Start with: "free", "no obligation", "this week", "Lighthouse", "SEO", "I couldn't find", "you don't have a website", "rating", "reviews", "stars", emoji, "!". Mirrors the Serbian list. |
| Examples | `config/outreach.md`: Serbian only, with automotive/construction niche sections (the failed niche) | `config/outreach.sr.md` and `config/outreach.en.md`, organized by channel with `### generic` sections. Niche subsections are optional and matched against the run's niche name. A first draft of both files exists (script_edits.md section 10); approve the English one before the first UAE run. Without a file, drafts run without examples and `draft-notion` prints a warning. |
| Questions / niche link | "Questions by business type" section and niche links inside the examples | Taken from the niche definition (question, landing URL), otherwise generic. |
| Maps | `maps.language: ""`, Serbian areas only | `language` from the market (`en` for UAE). Add UAE areas. OSM in the UAE: area names are ambiguous, so use the bbox, and prefer `name:en` when present. |
| Notion | no market or language | `Market`, `Language` properties; `Industry` kept and holds the niche name. One review view per market. **Migration: set `Market = Serbia`, `Language = sr` on all existing pages** (explicitly, so the code never has to assume a default). Hand-made pages need Market and Industry set; `draft-notion` skips and reports pages without them. |
| Prices (when turned on) | one `price_from` string | Per market: EUR (SR) / AED (UAE), from the website price lists. |
| Signature | one string | Per language, if needed. |

## D. Remaining gaps from the project docs (still valid)

| # | Gap | Fix |
| --- | --- | --- |
| S5 | The first message doesn't offer the **free audit**, which every ONIX page is built around | Offer slot per language in the rule sets, controlled by `outreach.first_offer`. It stays off until website P0 proof is live. |
| S6 | Website plan: **proof before more outreach** | Go-live gate in the build order: no sends until website P0 (demo, sample audit, About, audit pages) is done. |
| S8 | **Runs ads** (Meta Pixel, Google Ads tag, GTM) | Detect it in the audit probe HTML. Viability +, reason "runs ads". |
| S9 | **Viber / WhatsApp link on site** | Detect `viber://`, `wa.me`, `api.whatsapp.com`. A Need item: "no tap-to-call / messaging link". |
| S11 | Size (chairs/doctors) has no data source | Leave it to the send step (`Verdict = Skip`, `Skip Reason: too small`). Don't fake a size score. |
| S12 | Public institutions (Serbia: "dom zdravlja", "zavod", "opšta bolnica", "KBC", "institut") | `gates.exclude_name_patterns` **per market**, empty for a new market until you add some. |
| S14 | Google Business Profile completeness (secondary categories, review replies) | Not in the score. Part of the one-page audit for shortlisted leads. |
| S15 | Niche-specific Need (for accommodation a booking widget means "already solved") | Optional `need_overrides` on the niche definition, e.g. `has_booking: solved`. Default: the generic Need rules. |
| S16 | The audit script / public scanner from the website docs overlaps ~70% with `audit.py` + signals | **One shared audit core** used by enrich, an `audit-report` command (one-page audit per lead) and later the `/provera-sajta` scanner. Language of the report follows the lead's market. |
| S17 | Per-lead `?ref=` links + UTM convention | `lead_ref` (e.g. `L042`) at sync + Notion `Ref`. UTM campaign built from market + niche, e.g. `uae-dental-w1`. |
| S18 | 10 validation conversations recorded in Notion | `Booking Method`, `Runs Ads`, `Discovery Notes`. |
| S19 | A/B test metrics and the decision rule (≥ 8 replies and ~2× the other arm). ~3 replies per 60 audits makes reply-based retuning a long way off | `Outcome` funnel stages (`audit_sent → replied → call → proposal → won/lost`) + `Price Challenged`. `score-report` compares **markets** (Serbia vs UAE) and niches, applies the docs' rule, and prints "not enough data" on small samples. Send/skip decisions (L2) are the main feedback early on. |
| S21 | Cold email from onixweblab.com risks the main domain | Email is follow-up only until a separate sending domain exists. That's a per-market channel setting. |
| S22 | Website plan: first touches by hand | Keep it in the principles. If the Instagram DM automation project is used, limit it to replies. |
| S23 | Never mention the patient-records platform | Add its terms to both languages' banned phrases. |

Volume note: drafting is automatic, so the limit is how many drafts you can send per day; tune `auto_draft_min_tier` (A/B/C) to match.

## E. Build order

1. **Market profiles + niche-on-go** (§A, §C config parts): `go` prompts, run manifest, `Lead.market/language/niche/niche_label`, batch format. Remove `collect.categories`, `industry_labels`, the country defaults. No scoring change yet.
2. **Shared `pipeline/channels.py` with market rules** (mobile prefixes, order, first-touch exclusions, labels).
3. **Audit split + signals** (incl. ads pixels, messaging links) as the **shared audit core**.
4. Collect: Maps category, listing count, status, Maps language per market, UAE areas.
5. **Scorer v4**: no industry Fit, niche-match check, per-market reach/review bands, per-market name-pattern gates.
6. **Notion**: `Market`, `Language` (Industry kept), `Tier`, `Priority`, `Score Reasons`, `Verdict` (optional, Skip only), `Skip Reason`, Status `Draft failed`, `Outcome`, `Ref`, `Booking Method`, `Runs Ads`, `Discovery Notes`, `Price Challenged`. Migration (Market = Serbia, Language = sr, no Verdict migration needed), then `--rescore`.
7. **Drafting rule sets** (sr, en), examples files per language (first drafts written 2026-11-26, you approve), offer slot, banned-terms update.
8. `audit-report` command from the audit core.
9. **Auto-drafting** (script_edits.md §11): draft at the end of every run, tier ≥ B, no approval or prompts; old draft-before-review paths replaced. D2 dropped (no "no website always outranks" cap).
10. `score-report`: per market and per niche, the docs' A/B rule.
11. **Go-live gate:** website P0 done before the first send in either market.
