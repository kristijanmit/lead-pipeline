# Outreach drafting

Drafting replaces the former `draft-outreach` Notion skill for steps 0-2 (queue, channel, first message).
Follow-ups, Follow-up due flags and Dead tracking (skill step 3) are **not** built yet (Phase B).
Nothing is ever sent: the pipeline only writes text for you to review and send.

## Workflow (automatic, script_edits.md §11, v2 step 9)
    run / batch / go (Lead Gen):  collect -> enrich -> score -> sync -> draft
                                  draft = this run's pages at Tier >= outreach.auto_draft_min_tier (B)
    you: open Notion, send (edit if needed), or set Verdict = Skip + Skip Reason
    draft-notion                  catch-up: pages left undrafted, hand-made pages, --redraft

No approval step, no prompt and no required note. `run` and `batch` never ask anything; `go` asks only what defines
the run (market, niche, source, location) plus one Start? confirm, which `go --yes` skips.

The draft step runs after sync and writes to the existing page with `update_page`. It drafts from the run's JSONL
lead (it carries `niche_label`, `profile_facts` and the run's niche question), joined to its page by `page_id` from
the sync cache, with `SM Audit` and a hand-set `Suggested Channel` taken from the page. Which pages: the run's synced
leads whose page passes the eligibility filter below. `--limit N` caps the pages drafted.

**Eligibility** (§11.2, `NotionSink.query_draftable`): `Qualified` checked, Status `Not started` (or blank), an empty
`Draft Message`, `Verdict` not `Skip`, `Tier` empty or at least `outreach.auto_draft_min_tier` (default `B`), and
`Market` + `Industry` set. An empty Tier stays eligible (hand-made pages have none). `SM Audit` is optional; when
present it is the first praise source. Set `Verdict = Skip` and drafting never touches the page.

**Never stop, never ask** (§11.4):
| Situation | What happens |
| --- | --- |
| message fails validation after one retry | Status `Draft failed`, reason in `Draft Notes`, next page drafted. `--redraft` picks it up again |
| claude CLI error / Notion write error on one page | reason in `Draft Notes` (and `Lead.errors`), Status unchanged so the page stays eligible, next page drafted |
| out of AI credits | drafting stops; the report says "N pages undrafted"; `run` exits 3 after sync. `batch` finishes later entries (collect → sync) without drafting and exits 3. No resume state: the pages keep an empty Draft Message, so `draft-notion` drafts them |
| page / lead without a market, or a language with no rule set | skipped and counted, never guessed |
| no usable contact data | no draft, counted as `no_contact_data` |

`outreach.auto_draft: false` turns the draft step off; `draft-notion` then runs dry unless `--yes`.

## What it writes (Notion)
| Property | Written when |
| --- | --- |
| `Status` = Queued | a draft was written (from `Not started`, blank or `Draft failed`) |
| `Status` = Draft failed | the message failed validation after its retry |
| `Suggested Channel` | a draft was written |
| `Draft Message` | a draft was written |
| `Draft Notes` | the "verify before sending" note (always sent, so a redraft clears an old note), or the failure reason |

Never written: `Chosen Channel`, `Last Contacted`, `Communication`, `Notes`, `Contact Name`, `Follow-up date`,
`Verdict`, `Skip Reason`. The draft is also stored on the run's JSONL lead. Edits you make in Notion are not copied
back to the JSONL.

**Feedback without a review step** (§11.6): sent as is = good; edited before sending = keep the edit; not worth
sending = `Verdict = Skip` + `Skip Reason`. `score-report` reads send/skip rates by tier, niche and market (docs/SCORING.md §11). Log every send as
`YYYY-MM-DD · SENT · channel · text` in `Communication`: the reply tables take the channel from that line.

## Catch-up (`draft-notion`)
    python -m pipeline.runner draft-notion [--limit N] [--company X] [--redraft] [--dry-run] [--yes]

Drafts every eligible page in the database: pages a run left undrafted (credits out, Notion error), hand-made pages,
and with `--redraft` pages that already have a draft (Status `Queued` / `Draft failed` too). Writes by default when
`outreach.auto_draft` is on; `--dry-run` only prints. Pages without `Market` or `Industry` are skipped and reported
("set Market and Industry on the page"). `--limit N` caps the pages drafted, not the pages read.

A page synced by a run is drafted from its latest JSONL row (sync cache key or a shared email, like `--rescore`;
any scored row: `scored`, `disqualified` or `synced`, so re-scoring a run doesn't cut it off),
so it keeps `profile_facts`, `niche_label` and the run's niche question; `SM Audit` comes from the page. A page with
no row (hand-made, or a cache rebuilt from Notion) is drafted from the page itself: create it with `Company`,
`Industry`, `Market`, `Location`, `Social Links` (the Instagram URL as a link), `SM Audit` (optional) and
`Source` = Manual. It does not touch `seen_domains.json` or the run dirs.

## Praise source (`profile_facts`, §11.3)
In this order: your `SM Audit` note → `profile_facts` → the plain line ("sviđa mi se kako radite" / "I like the way
you present your services") with `Draft Notes` "no concrete detail found". Nothing is invented.

`profile_facts` (schema v14) is built in enrich by `pipeline/enrichers/profile.py` (pure, no extra request) from the
homepage HTML the contact step already fetched and the Maps fields collect kept, most praise-worthy first: Maps
description (gosom's `descriptions` column, `Lead.maps_description`), site meta description, services named in
h1–h3 headings, site title, Maps category. Generic headings (kontakt, o nama, usluge, početna, contact, about, home,
services, cookies/kolačići, menu/meni…), the bare company name and duplicates are dropped; at most 5 facts of ~80
characters each, labelled ("Named on the website: Implantologija"). Leads enriched before v14 get what the Lead alone
gives (Maps description and category) at draft time. A fact the draft relies on is named in `Draft Notes` to check.
Not used yet: OSM tags (e.g. `healthcare:speciality`) and a Maps photo count (gosom's `images` column was empty in
the real CSV and is a scraped sample, not Google's count). Instagram bios stay out (login wall, ToS).

## Channel (`pipeline/channels.py`, pure; re-exported by `pipeline/drafting/channel.py`)
The lead's market (config.yaml `markets:`) decides the order, what counts as a mobile number and which
channels are never a first touch. `best_channel(lead, market)` returns the first usable channel in
`market.channels` that isn't in `first_touch_excluded`; `usable_channels` lists them all, excluded ones too.

- Serbia: Instagram > Facebook > Viber/Whatsapp (mobile `+3816…`) > Email > Call.
- UAE: Instagram > Facebook > LinkedIn > Email > Call. WhatsApp (mobile `+9715…`) is listed but never
  suggested for a first touch.

Phones are normalized with the market's dialing code first (`050…` in the UAE → `+97150…`, `00…` →
`+…`). The mobile channel's label is the market's own (`Viber/Whatsapp` / `WhatsApp`). LinkedIn comes from
the `linkedin` social link. Placeholder phones/emails are ignored. No usable contact data: no draft,
counted as `no_contact_data` in the manifest.

A lead or Notion page without a market is skipped, never guessed (counted in `skipped_market`). The Notion
`Market` option is the preset id (`serbia`, `uae`); it is read lowercased.

A market whose `language` has no rule set is skipped the same way ("no drafting rules for language …").

## Rule sets per language (`pipeline/drafting/rules_sr.py`, `rules_en.py`)
The market's `language` picks the rule set: system prompt, prompt rules, banned phrases, greeting per channel,
script check. `pipeline/drafting/rules.py` holds the shared `Rules` type and the validator. The validator
(`validate_message(message, channel, rules)`) re-checks the hard rules and retries once, naming the problem. A
second failure goes to `Lead.errors` (prefix `draft: invalid:`) and sets the page's Status to `Draft failed`.

Common to both languages:
- **Praise** comes from your note (`SM Audit`) first, then `profile_facts`, and your wording sets the strength.
  With neither, the plain line is used ("sviđa mi se kako radite" / "I like the way you present your services"),
  plus `draft_notes` "no concrete detail found". Nothing is invented.
- **Never**: Google rating, reviews or stars; "you don't have a website" / "couldn't find"; criticism of an
  existing site; Lighthouse numbers; price; a named client; emoji; "!"; an unfilled `{slot}` from the examples.
- **Exception, friendly warning**: a concrete, checkable problem from your note or the audit notes goes before
  the question, and `draft_notes` says to verify it.
- **Question** (niche-neutral): the niche's own `question` from `go`, when the run has one (read from the run's
  manifest, also by `draft-notion` for pages with a JSONL row). Otherwise one picked from the examples' Questions
  section and adapted to the business; hand-made pages always use the Questions list.
- **Sender line**, word for word: sr "pravim sajtove za lokalne firme" (Viber: "…za lokalne biznise"); en "I build
  websites for local businesses" or "…for clinics". The validator checks the clause after "Ja sam / ovde / I'm /
  this is Kristijan," and rejects anything added to it (e.g. "i online zakazivanje", "and booking systems").
- **Patient-records platform**: never mentioned. `rules.PLATFORM_TERMS` (patient records, medical records,
  EHR, EMR, karton pacijenta, elektronski karton, medicinska dokumentacija, evidencija pacijenata…) is matched as
  whole words, in both languages and on every channel.

Serbian (`sr`): Latin script with diacritics, capitalized "Vi" form. The DM shape is "Pozdrav, naišao sam na
Vaš profil (Facebook: stranicu) i [pohvala]. Ja sam Kristijan, pravim sajtove za lokalne firme, pa me zanima
[jedno pitanje]?". Email and Call open with "Dobar dan". Banned: "bez obaveze", "besplatno", "ove nedelje",
"lighthouse", "seo", "nisam uspeo/pronašao", "nemate sajt", "unapređujem", "zvezdic…", "recenzij…", "ocen…".
`Viber/Whatsapp` (Serbia's mobile label) has its own format: Google Maps opener, free-example offer, may cite
"dosta ocena", niche link line, and a redesign variant for leads with a site. A review claim ("dosta ocena",
"recenzije") is allowed only when `review_count` ≥ the market's `review_strong` (Serbia 50). Below that, the
prompt says to praise photos or work, or nothing, and the validator rejects any review mention, on Viber too.

English (`en`, UAE): "Hi" for DMs and LinkedIn, "Hello" for Email and Call. Latin script only (no Cyrillic or
Arabic). Exactly one question, and no link: the Email signature line is exempt, and links are allowed when
`link_in_first_message` is on. Banned (whole word): free, no obligation, this week, Lighthouse, SEO, AI,
automation, couldn't find, don't have a website, rating, review, stars, Dear Sir, hope this finds you,
hope you're well.

**LinkedIn** has two outputs, both stored in `Draft Message`:

    Connection note:
    <≤300 characters, no pitch, no question needed>

    After accept:
    <DM-shaped message, one question last>

**`outreach.first_offer`**: `question` (default) or `audit`. With `audit`, an Instagram first message ends with
an offer of a one-page note with concrete suggestions, phrased as a question. It uses the `## Instagram
(DM) with audit offer` examples, and "free" stays banned, so the work is described, never priced. Other channels
keep the question, and so does every lead without an audited site that answered: only `lead_type` redesign /
unclear with `site_reachable` and at least one Need item (`scorer.need_items`, niche `need_overrides` applied; the
items the audit report lists) gets the offer (`drafter.offer_for`); new_build, dead-domain, certificate-error,
didn't-load and "site looks fine" leads keep the question. Hand-made Notion pages don't carry `site_reachable`, so
their drafts always use the question. Leave it on `question` until website P0 (sample audit, About, review page) is live.

## Settings (`config.yaml` -> `outreach`)
`link_in_first_message`, `site_link`, `named_references`, `price_from`, `signature`, `examples_path`
(default `config/outreach.{language}.md`; must contain `{language}`), `first_offer` (`question` / `audit`),
`auto_draft` (default true: draft step in run/batch/go, `draft-notion` writes without `--yes`),
`auto_draft_min_tier` (A/B/C, default B: the lowest Tier drafting picks up; A = fewer, safer drafts, C = more volume).
Link, references and price stay off until set. With links on, the run niche's `landing_url` replaces `site_link`. The niche phrase in the prompt is the run's `niche_label` (the
`phrase` from the niche created in `go`). Sync writes it to the Notion `Niche Label` property and `draft-notion`
reads it back; a page without one falls back to `Industry` (the niche name). `Score Reasons` is read back too but
never reaches the prompt.
Voice references live in `config/outreach.{language}.md`, one file per drafting language (`outreach.sr.md`,
`outreach.en.md`). They hold your approved messages; edit them to change the tone. A missing file prints a
warning, and drafts run without examples. HTML comments (`<!-- -->`) are stripped before the model sees the file.
Layout: `## <channel>` sections (Instagram (DM), Facebook, LinkedIn, Email, Viber/Whatsapp, SMS / call or Call; Viber/Whatsapp falls back to the Instagram one when it has no section of its own, and has its own prompt rules: Google Maps opener, free-example offer, niche link),
each with `### alias | alias` niche subsections matched against the lead's niche name, and a fallback
subsection with a `generic` alias. LinkedIn always keeps its `### connection note` subsection, and its after-accept
subsections are `### after accept | <niche aliases>` / `### after accept | generic`. `… with audit offer` sections are
used only with `first_offer: audit`.
`## Viber/Whatsapp (redesign)` is used for leads that already have a site. `## Instagram (DM) with a friendly warning` and `## Questions...` ride along for every DM. Only the lead's
channel and niche are sent to the model; a file without these headings is sent whole. To add a niche, add a
`### name | alias` subsection with 2-3 approved messages.

## Audit report (`audit-report`, `pipeline/reports/`)
The one-page note with concrete suggestions (at most 3) that the audit-offer message promises (v2 step 8).

    python -m pipeline.runner audit-report --run <run_id> [--domain X] [--min-tier C] [--limit N] [--rescan] [--force]

- **Which leads:** qualified leads at `--min-tier` (default C) or better, best priority first; `--domain` picks one
  lead whatever its tier. Skipped (counted in the manifest): no website / social page only, domain dead
  (`dns`/`refused`), site didn't load, not audited (old run), and **"site looks fine"** (no findings). An empty
  report is never written. A certificate error is a finding.
- **Findings:** the scorer's own Need items (`scorer.need_items`), ranked by their Need points
  (`scoring.need.redesign`, with the niche's `need_overrides`; items at 0 are left out), at most 3. "Outdated"
  names the © year or the legacy builder. "What already works" lists only signals known to be true.
- **Language:** the lead's `language` (its market's), else the run manifest's market, else the market the run was scored under (`score --market`, runs from before markets). Wording in
  `pipeline/reports/texts_{sr,en}.py`: written for the owner, no tool names, scores, ratings or "free".
- **Data:** the run's stored audit by default. The report shows the audit date (the run's last enrich, or a full
  `enrich --only audit --rescan`), and the CLI warns when it is older than 14 days. `--rescan` re-audits each site
  with the shared audit core (`pipeline/audit.py`) and dates the report today; it does not change the JSONL.
- **Files:** `data/runs/<run_id>/reports/<domain>.md` and `<domain>.html` (self-contained, inline CSS, ONIX name,
  prints to one A4 page). The .md is the source: it carries a `<!-- add your own finding -->` placeholder (not
  rendered while empty; replace it with e.g. `4. **Title**` plus an indented line). A re-run keeps an existing .md
  (hand edits safe) and re-renders its HTML from it; `--force` rebuilds the .md from data.
- Google Business Profile completeness (S14) is not in the report: there is no data source for it yet.

## Cost
One `claude -p` call per lead using your Claude login (no API key), ~1k tokens each, `draft.model` (haiku).
