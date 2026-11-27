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

Until v2 step 7:
- Leads in an `en` market (UAE) are skipped, not drafted: "English drafting arrives in step 7". They are
  counted (`skipped_market`, `market_skip_reasons` in the manifest) and printed. A lead with a market that
  isn't in config is skipped the same way. LinkedIn is in the ranking, but its two outputs (connection note,
  after-accept message) arrive in step 7.

## Message rules (`pipeline/drafting/drafter.py`)
Serbian, Latin script, capitalized "Vi" form. DM shape: "Pozdrav, naišao sam na Vaš profil (Facebook: stranicu) i
[pohvala]. Ja sam Kristijan, pravim sajtove za lokalne firme, pa me zanima [jedno pitanje]?" Email and call
open with "Dobar dan, ovde Kristijan". 2-3 sentences, one easy question, always last.
- **Praise** comes only from your note (`SM Audit`) or the bio; your wording sets the strength. No note: the
  plain "sviđa mi se kako radite" plus a `draft_notes` hint to add a concrete detail. Nothing is invented.
- **Never**: Google rating or review count, "nemate sajt" / "nisam uspeo da pronađem", criticism of an existing
  site, Lighthouse numbers, "pravim i unapređujem", price, offer, link, named client, emoji, exclamation marks.
- **Exception, friendly warning**: a concrete checkable problem stated in your note or the audit notes (dead
  bio link, "My WordPress") goes before the question; `draft_notes` says to verify it before sending.
- **Question** is picked by business type: open, Google with a concrete service, price/term, has-a-site, or a
  bio angle (school, self-service wash, shop). A non-detailing business does not get the detailing template.
Code re-checks the hard rules (`validate_message`: greeting per channel, a question present, no rating/banned
phrases/emoji/Cyrillic) and retries once with the problem named; a second failure goes to `Lead.errors`.
`draft_notes` carries a "verify before sending" line, printed by the `draft` command.
Follow-ups and reply help are not part of the stage (Phase B).

## Settings (`config.yaml` -> `outreach`)
`link_in_first_message`, `site_link`, `named_references`, `price_from`, `signature`, `examples_path`,
`auto_draft_min_tier` (A/B/C, default B: the lowest Tier drafting picks up).
Link, references and price stay off until set. The niche phrase in the prompt is the run's `niche_label` (the
`phrase` from the niche created in `go`). Sync writes it to the Notion `Niche Label` property and `draft-notion`
reads it back; a page without one falls back to `Industry` (the niche name). `Score Reasons` is read back too but
never reaches the prompt.
Voice references live in `config/outreach.md` (your approved messages): edit that file to change the tone.
Layout: `## <channel>` sections (Instagram (DM), Facebook, Viber/Whatsapp, SMS / call; Viber/Whatsapp falls back to the Instagram one when it has no section of its own, and has its own prompt rules: Google Maps opener, free-example offer, niche link),
each with `### alias | alias` niche subsections matched against the lead's industry and a `### generic` fallback.
`## Viber/Whatsapp (redesign)` is used for leads that already have a site. `## Instagram (DM) with a friendly warning` and `## Questions...` ride along for every DM. Only the lead's
channel and niche are sent to the model; a file without these headings is sent whole. To add a niche, add a
`### name | alias` subsection with 2-3 approved messages.

## Cost
One `claude -p` call per lead using your Claude login (no API key), ~1k tokens each, `draft.model` (haiku).
