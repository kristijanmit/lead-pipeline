# Outreach drafting

The `draft` stage replaces the former `draft-outreach` Notion skill for steps 0-2 (queue, channel, first
message). Follow-ups, Follow-up due flags and Dead tracking (skill step 3) are **not** built yet (Phase B).
Nothing is ever sent: the pipeline only writes text for you to review.

## Workflow
    collect -> enrich -> score -> sync        (`run` / `batch` stop here; no drafting)
    you: open Notion, add an `SM Audit` note to the leads worth contacting
    draft-notion [--limit N] [--yes]          (dry run first, then --yes)
    you: review the drafts in Notion, send by hand

`draft-notion` reads pages with Status `Not started` / `Queued`, an empty `Draft Message` and a filled
`SM Audit` (your note is the gate, so no AI budget goes to leads you skipped). `--all` also drafts pages without
a note (generic praise); `--redraft` rewrites existing drafts, e.g. after you improved a note. Drafting inside
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

## Channel (`pipeline/drafting/channel.py`, pure)
Instagram link > Facebook link > Viber/Whatsapp (Serbian mobile, no Instagram/Facebook) > Email > Call
(landline only). Placeholder phones/emails are ignored. No usable contact data: no draft, counted as
`no_contact_data` in the manifest.

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
`industry_labels` (industry key -> Serbian niche phrase). Link, references and price stay off until set.
Voice references live in `config/outreach.md` (your approved messages): edit that file to change the tone.
Layout: `## <channel>` sections (Instagram (DM), Facebook, SMS / call; Viber/Whatsapp reads the Instagram one),
each with `### alias | alias` niche subsections matched against the lead's industry and a `### generic` fallback.
`## Instagram (DM) with a friendly warning` and `## Questions...` ride along for every DM. Only the lead's
channel and niche are sent to the model; a file without these headings is sent whole. To add a niche, add a
`### name | alias` subsection with 2-3 approved messages.

## Cost
One `claude -p` call per lead using your Claude login (no API key), ~1k tokens each, `draft.model` (haiku).
