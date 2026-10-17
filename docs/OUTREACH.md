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
