# Lead pipeline v2 — handoff to Claude Code

## 1. Put the plan into the repo first

These files are in `docs/plan/v2/` (committed to master):

| File | Role for Claude Code |
|---|---|
| `PIPELINE.md` | target design (what v2 should look like) |
| `strategy_gaps.md` | decisions K1–K3 and **the build order (§E)**, which is the one to follow |
| `script_edits.md` | edits per file with code line refs (checked at commit `7dc42c6`); §9 and §10 override §1–8 where they disagree |
| `outreach.sr.md` | becomes `config/outreach.sr.md` (replaces `config/outreach.md`) |
| `outreach.en.md` | becomes `config/outreach.en.md` |

Before you start, settle these (the plan assumes the recommendations in `script_edits.md` §3):
- Decided: D2 drop the "no website always outranks" cap · D3 automatic drafting, no review gate (script_edits.md §11) · D4 two fields (Score Reasons + Audit Notes) · D5 obsolete.
- Edit and approve both outreach files.
- Open the UAE boxes (`script_edits.md` §10.4) on a map once.

## 2. Kickoff prompt (paste once, at the start of the first session)

```
Read CLAUDE.md, then docs/plan/v2/strategy_gaps.md, docs/plan/v2/PIPELINE.md and docs/plan/v2/script_edits.md.
This is the v2 plan for this repo. Rules:
- Follow the build order in strategy_gaps.md §E. One step per branch/PR, tests green after each step.
- script_edits.md §9–§10 override §1–§8 where they disagree. Line refs were checked at commit 7dc42c6; re-check
  them against the current code before editing.
- Decisions: no default niches (niche is always created in `go`); markets Serbia + UAE with sr/en drafting;
  agency lane removed; Notion `Industry` select is kept and holds the niche name (no `Niche` property);
  D2 dropped; drafting is automatic with no approval, prompt or required note (script_edits.md §11).
- Keep CLAUDE.md rules: never commit data/ or .env, no parallel Overpass queries, robots.txt before fetching,
  Maps is a supplement, scorer.py stays pure (no I/O).
- Never call the Notion API for writes, never run a real scrape, never send anything. Where a step needs Notion
  changes or a real Maps CSV, stop and tell me exactly what to do by hand.
- Before coding each step: give me a short plan (files, schema/version bumps, tests) and wait for my OK.
- After each step: run the full test suite, update the docs listed for that step, and summarize what changed.
Start with step 1 of §E.
```

## 3. One prompt per step (new session or after /clear)

Copy the whole block for the step you are on. "You do by hand" is for you, not for Claude Code.

### Step 1

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 1 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
Implement market profiles and niche-on-go: `markets:` in config with Serbia and UAE, the `go` prompts, `--market` / `--niche-file`, manifest blocks, Lead fields `market/language/niche/niche_label` (`industry` = niche name), batch format with full blocks. Remove `collect.categories`, `industry_labels`, country defaults. No scoring change. See script_edits.md §9.1–9.2, §9.4.
If the plan conflicts with the code, stop and tell me.
```

You do by hand: Run `go` once with a test niche and `--limit 3`

### Step 2

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 2 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
Create shared `pipeline/channels.py` with market rules (mobile prefixes, channel order, first-touch exclusions, labels). `drafting/channel.py` re-exports it. Add LinkedIn as a channel.
If the plan conflicts with the code, stop and tell me.
```

### Step 3

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 3 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
Audit split + signals as the shared audit core: probe and Lighthouse separate, signals from the probe HTML (site_reachable, copyright_year, viewport, booking, click-to-call, ads pixels, viber/wa links). Schema v10. Add `enrich --only audit --rescan`.
If the plan conflicts with the code, stop and tell me.
```

You do by hand: Re-run audit on one old run

### Step 4

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 4 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
Collect: `maps_category`, `listing_count`, `business_status`, rejected_domains.json skip, Maps language per market, UAE `maps.areas` from script_edits.md §10.4, OSM `name:en` for en markets.
If the plan conflicts with the code, stop and tell me.
```

You do by hand: Give it one real gosom CSV (Serbia) so it can check the column names

### Step 5

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 5 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
Scorer v4 per PIPELINE.md §3: L0 gates, niche-match check, Need/Viability/Reach, priority, tiers, score_reasons, confidence; per-market values; scoring_version 4. Then run v3 and v4 side by side on run <id> and show me the top 20 of each.
If the plan conflicts with the code, stop and tell me.
```

You do by hand: Pick the run; judge the top 20

### Step 6

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 6 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
Notion side: `to_notion_properties`, `lead_from_page`, `query_draftable` (auto-draft eligibility, script_edits.md §11.2), `sync --min-tier`, `sync --rescore`. Write me the exact list of Notion properties and options to add and the migration steps; don't touch Notion.
If the plan conflicts with the code, stop and tell me.
```

You do by hand: Add properties, run the migration (Market = serbia, Language = sr), then `sync --rescore --limit 10`

### Step 7

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 7 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
Drafting rule sets `rules_sr.py` / `rules_en.py`, examples per language from `config/outreach.{language}.md`, niche-neutral question rule, `outreach.first_offer` (default question), LinkedIn outputs, banned-terms update. See script_edits.md §10.1.
If the plan conflicts with the code, stop and tell me.
```

You do by hand: Move the approved outreach files into `config/`; run `draft-notion --dry-run --limit 1` on one sr and one en page

### Step 8

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 8 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
`audit-report` command from the audit core: one-page audit per lead, language from the market.
If the plan conflicts with the code, stop and tell me.
```

You do by hand: Read 2–3 reports

### Step 9

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 9 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
Automatic drafting per script_edits.md §11: draft step after sync in run/batch/go (this run's pages, tier ≥ B), no `input()` in run/batch, `go --yes`, failures never stop the run, credit-out leaves pages for the next run, `profile_facts` in enrich and the drafter, drop the D2 cap. Replace the old draft-before-review paths.
If the plan conflicts with the code, stop and tell me.
```

You do by hand: One `run --limit 5` end to end, then read the drafts and Draft Notes

### Step 10

```
Read CLAUDE.md and docs/plan/v2/*. We're on step 10 of strategy_gaps.md §E. Follow the rules in the kickoff prompt in docs/plan/v2/CLAUDE_CODE_HANDOFF.md §2.
`score-report`: send-rate / skip-rate by tier / market / niche, Skip Reason counts, the A/B rule from the docs with 'not enough data' on small samples.
If the plan conflicts with the code, stop and tell me.
```

You do by hand: After ~50 drafts

### Step 11: go-live gate

Not code: no sends until website P0 (demo, sample audit, About, review pages) is live.

## 4. Tips

- Keep each step in its own session; paste the step block fresh. Long sessions drift. The kickoff prompt (§2) is only needed once; each step block points back to its rules.
- After step 5, ask it to update `docs/SCORING.md` and the changelog with the D2 reason.
- Cleanup (remove `total_score` etc., schema 11) one version after step 6 is stable.
