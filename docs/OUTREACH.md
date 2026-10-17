# Outreach drafting

The `draft` stage replaces the former `draft-outreach` Notion skill for steps 0-2 (queue, channel, first
message). Follow-ups, Follow-up due flags and Dead tracking (skill step 3) are **not** built yet (Phase B).
Nothing is ever sent: the pipeline only writes text for you to review.

## What it writes (Notion)
| Property | Written when |
| --- | --- |
| `Status` = Queued | qualified lead with a draft (else `Not started`) |
| `Suggested Channel` | always for a drafted lead |
| `Draft Message` | always for a drafted lead |

Never written: `Chosen Channel`, `Last Contacted`, `Communication`, `Notes`, `Contact Name`, `Follow-up date`.
Only **new** Notion pages get these fields (existing pages are skipped by the dedupe cache), so run
`draft` before `sync`.

## Hand-made leads (`draft-notion`)
For leads found by hand (e.g. an Instagram page, no website): create the Notion page yourself with
`Company`, `Industry`, `Location`, `Social Links` (the Instagram URL as a link), `SM Audit` (your notes, a
rich_text column you add by hand), and `Source` = Manual. Then:

    python -m pipeline.runner draft-notion --limit 1        # dry run, prints the draft
    python -m pipeline.runner draft-notion --yes            # writes it

It drafts every Manual page with an empty `Draft Message`, writes `Suggested Channel` + `Draft Message`, and sets
`Status` = Queued only from `Not started`. It never overwrites a draft. The `SM Audit` text is passed to the
prompt as first-hand context. It does not touch `seen_domains.json` or the run dirs.

## Channel (`pipeline/drafting/channel.py`, pure)
Instagram link > Facebook link > Viber/Whatsapp (Serbian mobile, no Instagram/Facebook) > Email > Call
(landline only). Placeholder phones/emails are ignored. No usable contact data: no draft, counted as
`no_contact_data` in the manifest.

## Message rules (`pipeline/drafting/drafter.py`)
Serbian, Latin script, "Vi" form. Opening "Dobar dan, ja sam Kristijan, ..." + one real fact + at most one
true observation + one open question. No price, package, call request, link, named client, Lighthouse
numbers, "besplatno", "bez obaveze", date-relative phrases or exclamation marks. Code re-checks the hard
rules (`validate_message`) and retries once with the problem named; a second failure goes to `Lead.errors`.
`draft_notes` carries a "verify before sending" line, printed by the `draft` command.

## Settings (`config.yaml` -> `outreach`)
`link_in_first_message`, `site_link`, `named_references`, `price_from`, `signature`, `examples_path`,
`industry_labels` (industry key -> Serbian niche phrase). Link, references and price stay off until set.
Voice references live in `config/outreach.md`: edit that file to change the tone.

## Cost
One `claude -p` call per lead using your Claude login (no API key), ~1k tokens each, `draft.model` (haiku).
