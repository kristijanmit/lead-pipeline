# Outreach drafting

The `draft` stage replaces the former `draft-outreach` Notion skill for steps 0-2 (queue, channel, first
message). Follow-ups, Follow-up due flags and Dead tracking (skill step 3) are **not** built yet (Phase B).
Nothing is ever sent: the pipeline only writes text for you to review.

## Workflow
    collect -> enrich -> score -> sync        (`run` / `batch` stop here; no drafting)
    you: open Notion, add an `SM Audit` note to the leads worth contacting
    draft-notion [--limit N] [--yes]          (dry run first, then --yes)
    you: review the drafts in Notion, send by hand

`draft-notion` reads pages per script_edits.md §11.2 (v2 step 6): `Qualified` checked, Status `Not started` (or blank),
an empty `Draft Message`, `Verdict` not `Skip`, and `Tier` empty or at least `outreach.auto_draft_min_tier`
(default `B`). An empty Tier stays eligible because hand-made and backfilled pages have none. `SM Audit` is
optional: when present it is still the first praise source. Pages without `Market` or `Industry` are skipped and
reported ("set Market and Industry on the page"). `--limit N` caps the pages drafted, not the pages read.
`--redraft` also rewrites existing drafts (Status `Queued` / `Draft failed` too). Set `Verdict = Skip` on a page
and drafting never touches it. Drafting inside
`run` / `batch` is opt-in (`--draft`, or `draft: true` on a batch entry) and is the older flow: `draft --run`
before `sync`. Edits you make in Notion are not copied back to the run's JSONL.

## What it writes (Notion)
| Property | Written when |
| --- | --- |
| `Status` = Queued | qualified lead with a draft (else `Not started`) |
| `Suggested Channel` | always for a drafted lead |
| `Draft Message` | always for a drafted lead |
| `Draft Notes` | the "verify before sending" note; `draft-notion` always sends it, so a redraft clears an old note |

Never written: `Chosen Channel`, `Last Contacted`, `Communication`, `Notes`, `Contact Name`, `Follow-up date`.
With `draft --run` the fields are written only when a Notion page is created (existing pages are skipped by
the dedupe cache), so run it before `sync`. `draft-notion` updates existing pages directly.

## Notion pages (`draft-notion`)
Works for synced and hand-made pages alike. For leads found by hand (e.g. an Instagram page, no website): create the Notion page yourself with
`Company`, `Industry`, `Location`, `Social Links` (the Instagram URL as a link), `SM Audit` (your notes, a
rich_text column you add by hand), and `Source` = Manual. Then:

    python -m pipeline.runner draft-notion --limit 1        # dry run, prints the draft
    python -m pipeline.runner draft-notion --yes            # writes it

It drafts every eligible page (see Workflow), writes `Suggested Channel` + `Draft Message`, and sets
`Status` = Queued only from `Not started`. It overwrites a draft only with `--redraft`. The `SM Audit` text is
passed to the prompt as first-hand context, plus `Audit Notes`, `CMS`, `Lead Type`, `Industry` and contacts. It does not touch `seen_domains.json` or the run dirs.

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
second failure goes to `Lead.errors`.

Common to both languages:
- **Praise** comes only from your note (`SM Audit`) or the bio, and your wording sets the strength. With no
  note, the plain line is used ("sviđa mi se kako radite" / "I like the way you present your services"), plus a
  `draft_notes` hint to add a concrete detail. Nothing is invented.
- **Never**: Google rating, reviews or stars; "you don't have a website" / "couldn't find"; criticism of an
  existing site; Lighthouse numbers; price; a named client; emoji; "!"; an unfilled `{slot}` from the examples.
- **Exception, friendly warning**: a concrete, checkable problem from your note or the audit notes goes before
  the question, and `draft_notes` says to verify it.
- **Question** (niche-neutral): the niche's own `question` from `go`, when the run has one (`draft --run` reads it
  from the manifest). Otherwise one picked from the examples' Questions section and adapted to the business.
  Notion pages carry no niche definition, so `draft-notion` always uses the Questions list.
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
didn't-load and "site looks fine" leads keep the question. Notion pages don't carry `site_reachable`, so `draft-notion` drafts always use
the question. Leave it on `question` until website P0 (sample audit, About, review page) is live.

## Settings (`config.yaml` -> `outreach`)
`link_in_first_message`, `site_link`, `named_references`, `price_from`, `signature`, `examples_path`
(default `config/outreach.{language}.md`; must contain `{language}`), `first_offer` (`question` / `audit`),
`auto_draft_min_tier` (A/B/C, default B: the lowest Tier drafting picks up).
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
The one-page note with three concrete improvements that the audit-offer message promises (v2 step 8).

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
